import os
import time
import sys
import xmlrpclib
import traceback


def get_server(host,port):
    "start xmlrpc server on requested host:port"
    import SimpleXMLRPCServer
    import socket
    maxport=port+50
    while port<maxport:
        try: # TRY TO FIND AN OPEN PORT
            server=SimpleXMLRPCServer.SimpleXMLRPCServer((host,port),
                                                         logRequests=False)
            break
        except socket.error: # KEEP TRYING MORE PORTS
            port += 1
    if port>= maxport:
        raise socket.error('unable to find any open port up to %d' % maxport)
    print >>sys.stderr, "Running XMLRPC server on port %d..." % port
    return server,port


def safe_dispatch(self,name,args):
    """restrict calls to selected methods, and trap all exceptions to
    keep server alive!"""
    import datetime
    if name in self.xmlrpc_methods: # MAKE SURE THIS METHOD IS EXPLICITLY ALLOWED
        try: # TRAP ALL ERRORS TO PREVENT OUR SERVER FROM DYING
            print >>sys.stderr,'XMLRPC:',name,args,\
                  datetime.datetime.now().isoformat(' ') # LOG THE REQUEST
            m=getattr(self,name) # GET THE BOUND METHOD
            val=m(*args) # CALL THE METHOD
            sys.stderr.flush() # FLUSH ANY OUTPUT TO OUR LOG
            return val # HAND BACK ITS RETURN VALUE
        except SystemExit:
            raise  # WE REALLY DO WANT TO EXIT.
        except: # METHOD RAISED AN EXCEPTION, SO PRINT TRACEBACK TO STDERR
            traceback.print_exc(self.max_tb,sys.stderr)
    else:
        print >>sys.stderr,"safe_dispatch: blocked unregistered method %s" % name
    return False # THIS RETURN VALUE IS CONFORMABLE BY XMLRPC...
    

class FileDict(dict):
    "read key,value pairs as tab separated lines"
    def __init__(self,filename):
        dict.__init__(self)
        f=file(filename)
        for line in f:
            key,val=line.split()
            self[key]=val
        f.close()

def serve_forever(self,demonize=True):
    'start the service -- this will run forever'
    import datetime
    if demonize:
        if self.errlog is False: # CREATE AN APPROPRIATE ERRORLOG FILEPATH
            self.errlog=os.getcwd()+'/'+self.name+'.log'
        sys.stdout=file(self.errlog,'a') # DEMONIZE BY REDIRECTING ALL OUTPUT TO LOG
        sys.stderr=sys.stdout
    print >>sys.stderr,"START_SERVER:%s %s" %(self.name,datetime.datetime.
                                                   now().isoformat(' '))
    sys.stderr.flush()
    self.server.serve_forever()


class CoordinatorInfo(object):
    """stores information about individual coordinators for the controller
    and provides interface to Coordinator that protects against possibility of
    deadlock."""
    def __init__(self,name,url,user,priority,resources):
        self.name=name
        self.url=url
        self.user=user
        self.priority=priority
        self.server=xmlrpclib.ServerProxy(url)
        self.processors={}
        self.resources=resources
        self.start_time=time.time()
        self.allocated_ncpu=0
        self.new_cpus=[]

    def __iadd__(self,newproc):
        "add a processor to this coordinator's list"
        self.processors[newproc]=time.time()
        return self

    def __isub__(self,oldproc):
        "remove a processor from this coordinator's list"
        del self.processors[oldproc]
        return self

    def update_load(self):
        """tell this coordinator to use only allocated_ncpu processors,
        and to launch processors on the list of new_cpus.
        Simply spawns a thread to do this without danger of deadlock"""
        import threading
        t=threading.Thread(target=self.update_load_thread,
                           args=(self.allocated_ncpu,self.new_cpus))
        self.new_cpus=[] # DISCONNECT FROM OLD LIST TO PREVENT OVERWRITING
        t.start()
        
    def update_load_thread(self,ncpu,new_cpus):
        """tell this coordinator to use only ncpu processors,
        and to launch processors on the list of new_cpus.
        Run this in a separate thread to prevent deadlock."""
        self.server.set_max_clients(ncpu)
        if len(new_cpus)>0:
            self.server.start_processors(new_cpus) # SEND OUR LIST
        



class ResourceController(object):
    """Centralized controller for getting resources and rules for
    making them.
    """
    xmlrpc_methods={'load_balance':0,'setrule':0,'delrule':0,'report_load':0,
                    'register_coordinator':0,'unregister_coordinator':0,
                    'register_processor':0,'unregister_processor':0,
                    'get_resource':0,'acquire_rule':0,'release_rule':0,
                    'request_cpus':0,'setload':0,'retry_unused_hosts':0,
                    'get_status':0}
    _dispatch=safe_dispatch # RESTRICT XMLRPC TO JUST THE METHODS LISTED ABOVE
    max_tb=10
    def __init__(self,rc='controller',port=5000,overload_margin=0.6,
                 rebalance_frequency=1200,errlog=False):
        self.name=rc
        self.overload_margin=overload_margin
        self.rebalance_frequency=rebalance_frequency
        self.errlog=errlog
        self.rebalance_time=time.time()
        self.must_rebalance=False
        self.host=os.uname()[1]
        self.hosts=FileDict(self.name+'.hosts')
        self.getrules()
        self.getresources()
        self.server,self.port = get_server(self.host,port)
        self.server.register_instance(self)
        self.coordinators={}
        self.locks={}
        self.systemLoad={}
        for host in self.hosts: # 1ST ASSUME HOST EMPTY, THEN GET LOAD REPORTS
            self.hosts[host]=float(self.hosts[host])
            self.systemLoad[host]=0.0

    __call__=serve_forever

    def assign_load(self):
        "calculate the latest balanced loads"
        maxload=0.
        total=0.
        for c in self.coordinators.values():
            total+=c.priority
        for v in self.hosts.values():
            maxload+=v
        if total>0.:
            maxload /= float(total)
        for c in self.coordinators.values():
            c.allocated_ncpu=int(maxload * c.priority)
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def assign_processors(self):
        "hand out available processors to coordinators in order of need"
        margin=self.overload_margin-1.0
        free_cpus=[]
        for host in self.hosts: # BUILD LIST OF HOST CPUS TO BE ASSIGNED
            if host not in self.systemLoad: # ADDING A NEW HOST
                self.systemLoad[host]=0.0 # DEFAULT LOAD: ASSUME HOST EMPTY
            if self.systemLoad[host]<self.hosts[host]+margin:
                free_cpus+=int(self.hosts[host]+self.overload_margin
                               -self.systemLoad[host])*[host]
        if len(free_cpus)==0: # WE DON'T HAVE ANY CPUS TO GIVE OUT
            return False
        l=[] # BUILD A LIST OF HOW MANY CPUS EACH COORDINATOR NEEDS
        for c in self.coordinators.values():
            ncpu=c.allocated_ncpu-len(c.processors)
            if ncpu>0:
                l+=ncpu*[c]  # ADD c TO l EXACTLY ncpu TIMES
        import random
        random.shuffle(l) # REORDER LIST OF COORDINATORS RANDOMLY
        i=0 # INDEX INTO OUR l LIST
        while i<len(free_cpus) and i<len(l): # HAND OUT THE FREE CPUS ONE BY ONE
            l[i].new_cpus.append(free_cpus[i])
            i+=1
        return i>0 # RETURN TRUE IF WE HANDED OUT SOME PROCESSORS

    def load_balance(self):
        "recalculate load assignments, and assign free cpus"
        self.rebalance_time=time.time() # RESET OUR FLAGS
        self.must_rebalance=False
        self.assign_load() # CALCULATE HOW MANY CPUS EACH COORDINATOR SHOULD GET
        self.assign_processors() # ASSIGN FREE CPUS TO COORDINATORS THAT NEED THEM
        for c in self.coordinators.values():
            c.update_load() # INFORM THE COORDINATOR
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def getrules(self):
        import shelve
        self.rules=shelve.open(self.name+'.rules')

    def getresources(self):
        import shelve
        self.resources=shelve.open(self.name+'.rsrc')

    def setrule(self,rsrc,rule):
        "save a resource generation rule into our database"
        self.rules[rsrc]=rule
        self.rules.close() # THIS IS THE ONLY WAY I KNOW TO FLUSH...
        self.getrules()
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE
        
    def delrule(self,rsrc):
        "delete a resource generation rule from our database"
        try:
            del self.rules[rsrc]
        except KeyError:
            print >>sys.stderr, "Attempt to delete unknown resource rule %s" % rsrc
        else:
            self.rules.close() # THIS IS THE ONLY WAY I KNOW TO FLUSH...
            self.getrules()
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def setload(self,host,maxload):
        "increase or decrease the maximum load allowed on a given host"
        self.hosts[host]=float(maxload)
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def report_load(self,host,pid,load):
        "save a reported load from one of our processors"
        self.systemLoad[host]=load
        # AT A REGULAR INTERVAL WE SHOULD REBALANCE LOAD
        if self.must_rebalance or \
               time.time()-self.rebalance_time>self.rebalance_frequency:
            self.load_balance()
        if load<self.hosts[host]+self.overload_margin:
            return True  # OK TO CONTINUE
        else:
            return False # THIS SYSTEM OVERLOADED, TELL PROCESSOR TO EXIT

    def register_coordinator(self,name,url,user,priority,resources):
        "save a coordinator's registration info"
        try:
            print >>sys.stderr,'change_priority: %s (%s,%s): %f -> %f' \
                  % (name,user,url,self.coordinators[url].priority,priority)
            self.coordinators[url].priority=priority
        except KeyError:
            print >>sys.stderr,'register_coordinator: %s (%s,%s): %f' \
                  % (name,user,url,priority)
            self.coordinators[url]=CoordinatorInfo(name,url,user,priority,resources)
            self.must_rebalance=True # FORCE REBALANCING ON NEXT OPPORTUNITY
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def unregister_coordinator(self,name,url,message):
        "remove a coordinator from our list"
        try:
            del self.coordinators[url]
            print >>sys.stderr,'unregister_coordinator: %s (%s): %s' \
                  % (name,url,message)
        except KeyError:
            print >>sys.stderr,'unregister_coordinator: %s unknown:%s (%s)' \
                  % (name,url,message)
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def request_cpus(self,name,url):
        "return a list of hosts for this coordinator to run processors on"
        try:
            c=self.coordinators[url]
        except KeyError:
            print >>sys.stderr,'request_cpus: unknown coordinator %s @ %s' % (name,url)
            return [] # HAND BACK AN EMPTY LIST
        self.assign_load() # CALCULATE HOW MANY CPUS EACH COORDINATOR SHOULD GET
        self.assign_processors() # ASSIGN FREE CPUS TO COORDINATORS THAT NEED THEM
        new_cpus=tuple(c.new_cpus) # MAKE A NEW COPY OF THE LIST OF HOSTS
        del c.new_cpus[:] # EMPTY OUR LIST
        return new_cpus

    def register_processor(self,host,pid,url):
        "record a new processor starting up"
        try:
            self.coordinators[url]+= (host,pid)
            self.systemLoad[host] += 1.0 # THIS PROBABLY INCREASES LOAD BY 1
        except KeyError:
            pass
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def unregister_processor(self,host,pid,url):
        "processor shutting down, so remove it from the list"
        try:
            self.coordinators[url]-= (host,pid)
            self.systemLoad[host] -= 1.0 # THIS PROBABLY INCREASES LOAD BY 1
        except KeyError:
            pass
        self.load_balance() # FREEING A PROCESSOR, SO REBALANCE TO USE THIS
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def get_resource(self,host,pid,rsrc):
        """return a filename for the resource, or False if rule must be applied,
        or True if client must wait to get the resource"""
        key=host+':'+rsrc
        try: # JUST HAND BACK THE RESOURCE
            return self.resources[key]
        except KeyError:
            if key in self.locks:
                return True # TELL CLIENT TO WAIT
            else:
                return False # TELL CLIENT TO ACQUIRE IT VIA RULE

    def acquire_rule(self,host,pid,rsrc):
        "lock the resource on this specific host, and return its production rule"
        if rsrc not in self.rules:
            return False # TELL CLIENT NO SUCH RULE
        key=host+':'+rsrc
        if key in self.locks:
            return True # TELL CLIENT TO WAIT
        self.locks[key]=pid # LOCK THIS RESOURCE ON THIS HOST UNTIL CONSTRUCTED
        return self.rules[rsrc] # RETURN THE CONSTRUCTION RULE

    def release_rule(self,host,pid,rsrc):
        "client is done applying this rule, so now safe to give out the resource"
        key=host+':'+rsrc
        try:
            del self.locks[key] # rsrc CONSTRUCTED, SO REMOVE THE LOCK
        except KeyError:
            print >>sys.stderr,"attempt to release non-existent lock %s,%s:%d" \
                  %(host,rule,pid)
        self.resources[key]=self.rules[rsrc][0] # ADD THE FILE NAME TO RESOURCE LIST
        self.resources.close() # THIS IS THE ONLY WAY I KNOW TO FLUSH THIS...
        self.getresources()
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def retry_unused_hosts(self):
        "reset systemLoad for hosts that have no jobs running"
        myhosts={}
        for c in self.coordinators.values(): # LIST HOSTS WE'RE CURRENTLY USING
            for host,pid in c.processors:
                myhosts[host]=None # MARK THIS HOST AS IN USE
        for host in self.systemLoad: # RESET LOADS FOR ALL HOSTS WE'RE NOT USING
            if host not in myhosts:
                self.systemLoad[host]=0.0
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def get_status(self):
        """get report of system loads, max loads, coordinators, rules,
        resources, locks"""
        return self.name,self.errlog,self.systemLoad,dict(self.hosts),\
               [(c.name,c.url,c.priority,c.allocated_ncpu,len(c.processors),\
                 c.start_time) for c in self.coordinators.values()], \
                 dict(self.rules),dict(self.resources),self.locks




class Coordinator(object):
    """Run our script as Processor on one or more client nodes, using
    XMLRPC communication between clients and server.
    On the server all output is logged to name.log,
    and successfully completed task IDs are stored in name.success,
    and error task IDs are stored in name.error
    On the clients all output is logged to /usr/tmp/name_#.log.
    """
    xmlrpc_methods={'start_processors':0,'register_client':0,'unregister_client':0,
                    'report_success':0,'report_error':0,'next':0,
                    'get_status':0,'set_max_clients':0,'stop_client':0}
    _dispatch=safe_dispatch # RESTRICT XMLRPC TO JUST THE METHODS LISTED ABOVE
    max_tb=10 # MAXIMUM #STACK LEVELS TO PRINT IN TRACEBACKS
    max_ssh_errors=5 #MAXIMUM #ERRORS TO PERMIT IN A ROW BEFORE QUITTING
    python='python' # DEFAULT EXECUTABLE FOR RUNNING OUR CLIENTS
    def __init__(self,name,script,it,resources,port=8888,priority=1.0,rc_url=None,
                 errlog=False):
        self.name=name
        self.script=script
        self.it=iter(it) # MAKE SURE it IS AN ITERATOR; IF IT'S NOT, MAKE IT SO
        self.resources=resources
        self.priority=priority
        self.errlog=errlog
        self.host=os.uname()[1]
        self.user=os.environ['USER']
        try: # MAKE SURE ssh-agent IS AVAILABLE TO US BEFORE LAUNCHING LOTS OF PROCS
            a=os.environ['SSH_AGENT_PID']
        except KeyError:
            raise OSError(1,'SSH_AGENT_PID not found.  No ssh-agent running?')
        self.dir=os.getcwd()
        self.n=0
        self.nsuccess=0
        self.nerrors=0
        self.nssh_errors=0
        self.iclient=0
        self.max_clients=40
        if rc_url is None: # USE DEFAULT RESOURCE CONTROLLER ADDRESS ON SAME HOST
            rc_url='http://%s:5000' % self.host
        self.rc_url=rc_url
        self.rc_server=xmlrpclib.ServerProxy(rc_url) #GET CONNECTION TO RESOURCE CONTROLLER
        self.server,self.port = get_server(self.host,port) #CREATE XMLRPC SERVER
        self.server.register_instance(self) # WE PROVIDE ALL THE METHODS FOR THE SERVER
        self.clients={}
        self.pending={}
        self.already_done={}
        self.stop_clients={}
        self.logfile={}
        self.clients_starting={}
        try: # LOAD LIST OF IDs ALREADY SUCCESSFULLY PROCESSED, IF ANY
            f=file(name+'.success','r')
            for line in f:
                self.already_done[line.strip()]=None
            f.close()
        except IOError: # OK IF NO SUCCESS FILE YET, WE'LL CREATE ONE.
            pass
        self.successfile=file(name+'.success','a') # success FILE IS CUMMULATIVE
        self.errorfile=file(name+'.error','w') # OVERWRITE THE ERROR FILE
        self.done=False
        self.register()

    def __call__(self,*l,**kwargs):
        "start the server, and launch a cpu request in a separate thread"
        import threading
        t=threading.Thread(target=self.initialize_thread)
        t.start()
        serve_forever(self,*l,**kwargs)

    def initialize_thread(self):
        "run this method in a separate thread to bootstrap our initial cpu request"
        time.sleep(5) # GIVE serve_forever() TIME TO START SERVER
        self.rc_server.load_balance() # NOW ASK CONTROLLER TO REBALANCE AND GIVE US CPUS

    def start_client(self,host):
        "start a processor on a client node"
        if len(self.clients)>=self.max_clients:
            print >>sys.stderr,'start_client: blocked, too many already', \
                  len(self.clients),self.max_clients
            return # DON'T START ANOTHER PROCESS, TOO MANY ALREADY
        try:
            if len(self.clients_starting[host])>self.max_ssh_errors:
                print >>sys.stderr,\
                      'start_client: blocked, too many unstarted jobs:',\
                      host,self.clients_starting[host]
                return # DON'T START ANOTHER PROCESS, host MAY BE DEAD...
        except KeyError: # NO clients_starting ON host, GOOD!
            pass
        logfile='/usr/tmp/%s_%d.log' % (self.name,self.iclient)
        cmd='cd %s;%s %s --url=http://%s:%d --rc_url=%s --logfile=%s %s' \
             % (self.dir,self.python,self.script,self.host,self.port,
                self.rc_url,logfile,self.name)
        ssh_cmd="ssh %s '(%s) </dev/null >&%s &' &" % (host,cmd,logfile)
        print >>sys.stderr,'SSH: '+ssh_cmd
        self.logfile[logfile]=[host,False,self.iclient] # NO PID YET
        try: # RECORD THIS CLIENT AS STARTING UP
            self.clients_starting[host][self.iclient]=time.time()
        except KeyError: # CREATE A NEW HOST ENTRY
            self.clients_starting[host]={self.iclient:time.time()}
        # RUN SSH IN BACKGROUND TO AVOID WAITING FOR IT TO TIMEOUT!!!
        os.system(ssh_cmd) # LAUNCH THE SSH PROCESS, SHOULD RETURN IMMEDIATELY
        self.iclient += 1 # ADVANCE OUR CLIENT COUNTER

    def start_processors(self,hosts):
        "start processors on the list of hosts using SSH transport"
        for host in hosts: # LAUNCH OURSELVES AS PROCESSOR ON ALL THESE HOSTS
            self.start_client(host)
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def register(self):
        "register our existence with the resource controller"
        url='http://%s:%d' % (self.host,self.port)
        self.rc_server.register_coordinator(self.name,url,self.user,
                                            self.priority,self.resources)

    def unregister(self,message):
        "tell the resource controller we're exiting"
        url='http://%s:%d' % (self.host,self.port)
        self.rc_server.unregister_coordinator(self.name,url,message)

    def register_client(self,host,pid,logfile):
        'XMLRPC call to register client hostname and PID as starting_up'
        print >>sys.stderr,'register_client: %s:%d' %(host,pid)
        self.clients[(host,pid)]=0
        try:
            self.logfile[logfile][1]=pid # SAVE OUR PID
            iclient=self.logfile[logfile][2] # GET ITS CLIENT ID
            del self.clients_starting[host][iclient] #REMOVE FROM STARTUP LIST
        except KeyError:
            print >>sys.stderr,'no client logfile?',host,pid,logfile
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def unregister_client(self,host,pid,message):
        'XMLRPC call to remove client from register as exiting'
        print >>sys.stderr,'unregister_client: %s:%d %s' % (host,pid,message)
        try:
            del self.clients[(host,pid)]
        except KeyError:
            print >>sys.stderr,'unregister_client: unknown client %s:%d' % (host,pid)
        try: # REMOVE IT FROM THE LIST OF CLIENTS TO SHUTDOWN, IF PRESENT
            del self.stop_clients[(host,pid)]
        except KeyError:
            pass
        if len(self.clients)==0 and self.done: # NO MORE TASKS AND NO MORE CLIENTS
            self.exit("Done") # SO SERVER CAN EXIT
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def report_success(self,host,pid,success_id):
        'mark task as successfully completed'
        print >>self.successfile,success_id # KEEP PERMANENT RECORD OF SUCCESS ID
        self.successfile.flush()
        self.nsuccess += 1
        try:
            self.clients[(host,pid)] += 1
        except KeyError:
            print >>sys.stderr,'report_success: unknown client %s:%d' % (host,pid)
        try:
            del self.pending[success_id]
        except KeyError:
            print >>sys.stderr,'report_success: unknown ID %s' % str(success_id)
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def report_error(self,host,pid,id,tb_report):
        "get traceback report from client as text"
        print >>sys.stderr,"TRACEBACK: %s:%s ID %s\n%s" % \
              (host,str(pid),str(id),tb_report)
        try:
            del self.pending[id]
        except KeyError: # NOT ASSOCIATED WITH AN ACTUAL TASK ID, SO DON'T RECORD
            if id is not None and id is not False:
                print >>sys.stderr,'report_error: unknown ID %s' % str(id)
        else:
            print >>self.errorfile,id # KEEP PERMANENT RECORD OF FAILURE ID
            self.nerrors+=1
            self.errorfile.flush()
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE

    def next(self,host,pid,success_id):
        'return next ID from iterator to the XMLRPC caller'
        if success_id is not False:
            self.report_success(host,pid,success_id)
        if self.done: # EXHAUSTED OUR ITERATOR, SO SHUT DOWN THIS CLIENT
            return False # HAND BACK "NO MORE FOR YOU TO DO" SIGNAL
        try:  # CHECK LIST FOR COMMAND TO SHUT DOWN THIS CLIENT
            del self.stop_clients[(host,pid)] # IS IT IN stop_clients?
            return False # IF SO, HAND BACK "NO MORE FOR YOU TO DO" SIGNAL
        except KeyError: # DO ONE MORE CHECK: ARE WE OVER OUR MAX ALLOWED LOAD?
            if len(self.clients)>self.max_clients: # YES, BETTER THROTTLE DOWN
                print >>sys.stderr,'next: halting %s:too many processors (%d>%d)' \
                      % (host,len(self.clients),self.max_clients)
                return False # HAND BACK "NO MORE FOR YOU TO DO" SIGNAL
        for id in self.it: # GET AN ID WE CAN USE
            if str(id) not in self.already_done:
                self.n+=1 # GREAT, WE CAN USE THIS ID
                self.lastID=id
                self.pending[id]=(host,pid,time.time())
                print >>sys.stderr,'giving id %s to %s:%d' %(str(id),host,pid)
                return id
        print >>sys.stderr,'exhausted all items from iterator!'
        self.done=True # EXHAUSTED OUR ITERATOR
        self.priority=0.0 # RELEASE OUR CLAIMS ON ANY FURTHER PROCESSOR ALLOCATION
        self.register() # AND INFORM THE RESOURCE CONTROLLER
        return False # False IS CONFORMABLE BY XMLRPC...

    def get_status(self):
        "return basic status info on number of jobs finished, client list etc."
        client_report=[client+(nsuccess,) for client,nsuccess in self.clients.items()]
        pending_report=[(k,)+v for k,v in self.pending.items()]
        return self.name,self.errlog,self.n,self.nsuccess,self.nerrors,client_report,\
               pending_report,self.logfile
    def set_max_clients(self,n):
        "change the maximum number of clients we should have running"
        self.max_clients=int(n)  # MAKE SURE n IS CONVERTABLE TO int
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE
    def stop_client(self,host,pid):
        "set signal forcing this client to exit on next iteration"
        self.stop_clients[(host,pid)]=None
        return True  # USE THIS AS DEFAULT XMLRPC RETURN VALUE
    def exit(self,message):
        "clean up and close this server"
        self.unregister(message)
        self.successfile.close()
        self.errorfile.close()
        sys.exit()
        


class ResourceFile(file):
    """wrapper around some locking behavior, to ensure only one copy operation
    performed for a given resource on a given host.
    Otherwise, it's just a regular file object."""
    def __init__(self,resource,rule,mode,processor):
        "resource is name of the resource; rule is (localFile,cpCommand)"
        self.resource=resource
        self.processor=processor
        localFile,cpCommand=rule
        if not os.access(localFile,os.R_OK):
            cmd=cpCommand % localFile
            print 'copying data:',cmd
            os.system(cmd)
        file.__init__(self,localFile,mode) # NOW INITIALIZE AS A REAL FILE OBJECT

    def close(self):
        self.processor.release_rule(self.resource) # RELEASE THE LOCK WE PLACED ON THIS RULE
        file.close(self)





class Processor(object):
    'provides an iterator interface to an XMLRPC ID server'
    max_errors_in_a_row=10 # LOOKS LIKE NOTHING WORKS HERE, SO QUIT!
    max_tb=10 # DON'T SHOW MORE THAN 10 STACK LEVELS FOR A TRACEBACK
    report_frequency=600
    overload_max=5 # MAXIMUM NUMBER OF OVERLOAD EVENTS IN A ROW BEFORE WE EXIT
    def __init__(self,url="http://localhost:8888",
                 rc_url='http://localhost:5000',logfile=False):
        self.url=url
        self.logfile=logfile
        self.server=xmlrpclib.ServerProxy(url)
        self.rc_url=rc_url
        self.rc_server=xmlrpclib.ServerProxy(rc_url)
        self.host=os.uname()[1]
        self.pid=os.getpid()
        self.user=os.environ['USER']
        self.success_id=False
        self.pending_id=False
        self.exit_message='MYSTERY-EXIT please debug'
        self.overload_count=0
        
    def register(self):
        "add ourselves to list of processors for this server"
        self.server.register_client(self.host,self.pid,self.logfile)
        self.rc_server.register_processor(self.host,self.pid,self.url)
        print >>sys.stderr,'REGISTERED:',self.url,self.rc_url

    def unregister(self,message):
        "remove ourselves from list of processors for this server"
        if self.success_id is not False: # REPORT THAT LAST JOB SUCCEEDED!
            self.report_success(self.success_id)
        self.server.unregister_client(self.host,self.pid,message)    
        self.rc_server.unregister_processor(self.host,self.pid,self.url)
        print >>sys.stderr,'UNREGISTERED:',self.url,self.rc_url,message

    def __iter__(self):
        return self

    def next(self):
        "get next ID from server"
        # REPORT LAST JOB SUCCESSFULLY COMPLETED, IF ANY
        while 1:
            id=self.server.next(self.host,self.pid,self.success_id)
            self.success_id=False # ERASE SUCCESS ID
            if id is True: # WE'RE BEING TOLD TO JUST WAIT
                time.sleep(60) # SO GO TO SLEEP FOR A MINUTE
            else:
                break
        if id is False: # NO MODE id FOR US TO PROCESS, SO QUIT
            raise StopIteration
        else: # HAND BACK THE id TO THE USER
            self.pending_id=id
            return id

    def report_success(self,id):
        "report successful completion of task ID"
        self.server.report_success(self.host,self.pid,id)

    def report_error(self,id):
        "report an error using traceback.print_exc()"
        import StringIO
        err_report=StringIO.StringIO()
        traceback.print_exc(self.max_tb,sys.stderr) #REPORT TB TO OUR LOG
        traceback.print_exc(self.max_tb,err_report) #REPORT TB TO SERVER
        self.server.report_error(self.host,self.pid,id,err_report.getvalue())
        err_report.close()

    def report_load(self):
        "report system load"
        ofile=os.popen('uptime')
        line=ofile.readline() # SKIP TITLE LINE
        ofile.close()
        load=float(line.split()[-3][:-1]) # GET RID OF THE TERMINAL ,
        if self.rc_server.report_load(self.host,self.pid,load) is False:
            self.overload_count+=1 # ARE WE CONSISTENTLY OVERLOADED FOR EXTENDED PERIOD?
            if self.overload_count>self.overload_max: # IF EXCEEDED LIMIT, EXIT
                self.exit('load too high')
        else:
            self.overload_count=0

    def open_resource(self,resource,mode):
        "get a file object for the requested resource, opened in mode"
        while 1:
            rule=self.rc_server.get_resource(self.host,self.pid,resource)
            if rule is False: # WE HAVE TO LOCK AND APPLY A RULE...
                rule=self.acquire_rule(resource)
                if rule is True: # HMM, LOOKS LIKE A RACE CONDITION. KEEP WAITING
                    time.sleep(60)  # WAIT A MINUTE BEFORE ASKING FOR RESOURCE AGAIN
                    continue
                return ResourceFile(resource,rule,mode,self) #CONSTRUCT THE RESOURCE
            elif rule is True: # RULE IS LOCKED BY ANOTHER PROCESSOR
                time.sleep(60)  # WAIT A MINUTE BEFORE ASKING FOR RESOURCE AGAIN
            else: # GOT A REGULAR FILE, SO JUST OPEN IT
                return file(rule,mode)
            
    def acquire_rule(self,resource):
        "lock the specified resource rule for this host, so it's safe to build it"
        rule=self.rc_server.acquire_rule(self.host,self.pid,resource)
        if rule is False: # NO SUCH RESOURCE?!?
            self.exit('invalid resource: '+resource)
        return rule

    def release_rule(self,resource):
        "release our lock on this resource rule, so others can use it"
        self.rc_server.release_rule(self.host,self.pid,resource)

    def exit(self,message):
        "save message for self.unregister() and force exit"
        self.exit_message=message
        raise SystemExit
        
    def run_all(self,resultGenerator,**kwargs):
        "run until all task IDs completed, trap & report all errors"
        errors_in_a_row=0
        it=resultGenerator(self,**kwargs) # GET ITERATOR FROM GENERATOR
        report_time=time.time()
        self.register() # REGISTER WITH RESOURCE CONTROLLER & COORDINATOR
        try: # TRAP ERRORS BOTH IN USER CODE AND coordinator CODE
            while 1:
                try: # TRAP AND REPORT ALL ERRORS IN USER CODE
                    id=it.next() # THIS RUNS USER CODE FOR ONE ITERATION
                    self.success_id=id  # MARK THIS AS A SUCCESS...
                    errors_in_a_row=0
                except StopIteration: # NO MORE TASKS FOR US...
                    self.exit_message='done'
                    break
                except SystemExit: # sys.exit() CALLED
                    raise  # WE REALLY DO WANT TO EXIT.
                except: # MUST HAVE BEEN AN ERROR IN THE USER CODE
                    self.report_error(self.pending_id) # REPORT THE PROBLEM
                    errors_in_a_row +=1
                    if errors_in_a_row>=self.max_errors_in_a_row:
                        self.exit_message='too many errors'
                        break
                if time.time()-report_time>self.report_frequency:
                    self.report_load() # SEND A ROUTINE LOAD REPORT
                    report_time=time.time()
        except SystemExit: # sys.exit() CALLED
            pass  # WE REALLY DO WANT TO EXIT.
        except: # IMPORTANT TO TRAP ALL ERRORS SO THAT WE UNREGISTER!!
            traceback.print_exc(self.max_tb,sys.stderr) #REPORT TB TO OUR LOG
            self.exit_message='error trap'
        self.unregister('run_all '+self.exit_message) # MUST UNREGISTER!!

    def run_interactive(self,it,n=1,**kwargs):
        "run n task IDs, with no error trapping"
        if not hasattr(it,'next'):
            it=it(self,**kwargs) # ASSUME it IS GENERATOR, USE IT TO GET ITERATOR
        i=0
        self.register() # REGISTER WITH RESOURCE CONTROLLER & COORDINATOR
        try: # EVEN IF ERROR OCCURS, WE MUST UNREGISTER!!
            for id in it:
                self.success_id=id
                i+=1
                if i>=n:
                    break
        except:
            self.unregister('run_interactive error') # MUST UNREGISTER!!!
            raise # SHOW THE ERROR INTERACTIVELY
        self.unregister('run_interactive exit')
        return it # HAND BACK ITERATOR IN CASE USER WANTS TO RUN MORE...


def parse_argv():
    "parse sys.argv into a dictionary of GNU-style args --foo=bar and list of other args"
    d={}
    l=[]
    for v in sys.argv[1:]:
        if v[:2]=='--':
            try:
                k,v=v[2:].split('=')
                d[k]=v
            except ValueError:
                d[v[2:]]=None
        else:
            l.append(v)
    return d,l

def start_client_or_server(clientGenerator,serverGenerator,resources,script):
    """start controller, client or server depending on whether 
    we get coordinator argument from the command-line args.

    Client must be a generator function that takes Processor as argument,
    and uses it as an iterator.
    Also, clientGenerator must yield the IDs that the Processor provides
    (this structure allows us to trap all exceptions from clientGenerator,
    while allowing it to do resource initializations that would be
    much less elegant in a callback function.)

    Server must be a function that returns an iterator (e.g. a generator).
    Resources is a list of strings naming the resources we need
    copied to local host for client to be able to do its work.

    Both client and server constructors use **kwargs to get command
    line arguments (passed as GNU-style --foo=bar;
    see the constructor arguments to see the list of
    options that each can be passed.

    #CALL LIKE THIS FROM yourscript.py:
    import coordinator
    if __name__=='__main__':
      coordinator.start_client_or_server(clientGen,serverGen,resources,__file__)

    To start the resource controller:
      python coordinator.py --rc=NAME [options]

    To start a job coordinator:
      python yourscript.py NAME [--rc_url=URL] [options]

    To start a job processor:
      python yourscript.py --url=URL --rc_url=URL [options]"""
    d,l=parse_argv()
    if 'url' in d: # WE ARE A CLIENT!
        client=Processor(**d)
        time.sleep(5) # GIVE THE SERVER SOME BREATHING SPACE
        client.run_all(clientGenerator,**d)
    elif 'rc' in d: # WE ARE THE RESOURCE CONTROLLER
        rc_server=ResourceController(**d) # NAME FOR THIS CONTROLLER...
        rc_server() # START THE SERVER
    else: # WE ARE A SERVER
        server=Coordinator(l[0],script,serverGenerator(),resources,**d)
        server() # START THE SERVER


class CoordinatorMonitor(object):
    "Monitor a Coordinator."
    def __init__(self,coordInfo):
        self.name,self.url,self.priority,self.allocated_ncpu,self.ncpu,\
                 self.start_time=coordInfo
        self.server=xmlrpclib.ServerProxy(self.url)
        self.get_status()
    def get_status(self):
        self.name,self.errlog,self.n,self.nsuccess,self.nerrors,self.client_report,\
               self.pending_report,self.logfile=self.server.get_status()
        print "Got status from Coordinator:",self.name,self.url
    def __getattr__(self,attr):
        "just pass on method requests to our server"
        return getattr(self.server,attr)

class RCMonitor(object):
    """monitor a ResourceController.  Useful methods:
    get_status()
    load_balance()
    setrule(rsrc,rule)
    delrule(rsrc)
    setload(host,maxload)
    retry_unused_hosts()
    Documented in ResourceController docstrings."""
    def __init__(self,host=None,port=5000):
        if host is None:
            host=os.uname()[1]
        self.rc_url='http://%s:%d' %(host,port)
        self.rc_server=xmlrpclib.ServerProxy(self.rc_url)
        self.get_status()

    def get_status(self):
        self.name,self.errlog,self.systemLoad,self.hosts,coordinators, \
               self.rules,self.resources,self.locks=self.rc_server.get_status()
        print "Got status from ResourceController:",self.name,self.rc_url
        self.coordinators={}
        for cinfo in coordinators:
            self.coordinators[cinfo[0]]=CoordinatorMonitor(cinfo)

    def __getattr__(self,attr):
        "just pass on method requests to our rc_server"
        return getattr(self.rc_server,attr)

if __name__=='__main__':
    start_client_or_server(None,None,[],'no.script')