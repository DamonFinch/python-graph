"""
Test some of the basics underpinning the graph system.
"""

import os, unittest

from testlib import testutil, PygrTestProgram, SkipTest
from pygr import mapping, graphquery, sqlgraph

class Query_Test(unittest.TestCase):
    "Pygr Query tests"

    def dqcmp(self, datagraph, querygraph, result):
        try:
            g = self.datagraph
        except AttributeError:
            pass
        else:
            g.update(datagraph)
            datagraph = g
            
        l = [ d.copy() for d in graphquery.GraphQuery(datagraph, querygraph) ]
        assert len(l) == len(result), 'length mismatch'
        l.sort()
        result.sort()
        for i in range(len(l)):
            assert l[i] == result[i], 'incorrect result'
    
    def test_basicquery_test(self):
        "Basic query"
        datagraph = {0: {1: None, 2: None, 3: None},
                     1: {2: None}, 3: {4: None, 5: None},
                     4: {6: None}, 5: {6: None}, 2: {}, 6: {}}
        querygraph = {0: {1: None, 2: None, 3: None},
                      3:{4: None},1:{},2:{},4:{}}
        result = [{0: 0, 1: 1, 2: 2, 3: 3, 4: 4},
                  {0: 0, 1: 1, 2: 2, 3: 3, 4: 5},
                  {0: 0, 1: 2, 2: 1, 3: 3, 4: 4},
                  {0: 0, 1: 2, 2: 1, 3: 3, 4: 5}]
        
        self.dqcmp(datagraph, querygraph, result) 
    
    def test_cyclicquery(self): 
        "Cyclic QG against cyclic DG @CTB comment?"
        datagraph = { 1 :{2:None}, 2:{3:None}, 3:{4:None}, 4:{5:None},
                      5:{2:None}}
        querygraph = {0:{1:None}, 1:{2:None}, 2:{4:None}, 3:{1:None},
                      4:{3:None}}
        result = [ {0: 1, 1: 2, 2: 3, 3: 5, 4: 4} ]
        self.dqcmp(datagraph, querygraph, result)
    
    def test_cyclicacyclicquery(self):
        "Cyclic QG against acyclic DG"
        datagraph = {0:{1:None}, 1:{3:None}, 5:{3:None}, 4:{5:None},
                     2:{4:None,1:None}, 3:{}}
        querygraph = {0:{1:None}, 1:{3:None}, 3:{5:None}, 5:{4:None},
                      4:{2:None}, 2:{1:None}}
        result = []
        self.dqcmp(datagraph,querygraph,result)
    
    def test_symmetricquery_test(self):
        "Symmetrical QG against symmetrical DG"
        datagraph = {1:{2:None},2:{3:None,4:None},5:{2:None},3:{},4:{}}
        querygraph = {0:{1:None},1:{2:None},2:{}}
        result = [{0: 1, 1: 2, 2: 3}, {0: 1, 1: 2, 2: 4},
                  {0: 5, 1: 2, 2: 3}, {0: 5, 1: 2, 2: 4}]
        self.dqcmp(datagraph,querygraph,result)

    def test_filteredquery(self):
        "Test a filter against a query"
        datagraph = {0: {1: None, 2: None, 3: None}, 1: {2: None, 3: None},
                     3: {4: None}}
        querygraph = {0:{1:{'filter':lambda toNode,**kw:toNode == 3}},1:{}}
        result = [{0: 0, 1: 3},{0: 1, 1: 3}]
        self.dqcmp(datagraph,querygraph,result)

    def test_headlessquery(self):
        "Test a query with no head nodes"
        datagraph = {0:{1:None},1:{2:None},2:{3:None},3:{4:None},4:{1:None}}
        querygraph = {0:{1:None},1:{2:None},2:{3:None},3:{0:None}}
        result = [{0: 1, 1: 2, 2: 3, 3: 4},
                  {0: 2, 1: 3, 2: 4, 3: 1},
                  {0: 3, 1: 4, 2: 1, 3: 2},
                  {0: 4, 1: 1, 2: 2, 3: 3}]
        self.dqcmp(datagraph,querygraph,result)

class Mapping_Test(Query_Test):
    "Tests mappings"

    def setUp(self):
        self.datagraph = mapping.dictGraph()

    def test_graphdict(self):
        "Graph dictionary"
        datagraph = self.datagraph
        datagraph += 1 
        datagraph[1] += 2
        results = {1: {2: None}, 2: {}}
        assert datagraph == results, 'incorrect result'
    
    def test_nodedel(self): 
        "Node deletion"
        datagraph = self.datagraph
        datagraph += 1
        datagraph += 2 
        datagraph[2] += 3
        datagraph -= 1
        results = {2: {3: None}, 3: {}}
        assert datagraph == results, 'incorrect result'
    
    def test_delraise(self):
        "Delete raise"
        datagraph = self.datagraph
        datagraph += 1
        datagraph += 2
        datagraph[2] += 3
        try:
            for i in range(0,2):
                datagraph -= 3
            raise ValueError('failed to catch bad node deletion attempt')
        except KeyError:
            pass # THIS IS THE CORRECT RESULT

    def test_setitemraise(self):
        "Setitemraise"
        datagraph = self.datagraph
        datagraph += 1
        try:
            datagraph[1] = 2
            raise KeyError('failed to catch bad setitem attempt')
        except ValueError:
            pass # THIS IS THE CORRECT RESULT

    def test_graphedges(self): 
        "Graphedges"
        datagraph = self.datagraph
        graphvals = {1:{2:None},2:{3:None,4:None},5:{2:None},3:{},4:{}}
        edge_list = [[1, 2,None], [2, 3,None], [2, 4,None], [5, 2,None]]
        for i in graphvals:
            datagraph += i
            for n in graphvals[i].keys():
                datagraph[i] += n
        edge_results = []
        for e in datagraph.edges():
            edge_results.append(e)
        edge_results.sort()
        edge_results = [list(t) for t in edge_results]
        edge_list.sort()
        #print 'edge_results:',edge_results
        assert edge_results == edge_list, 'incorrect result'        

class Graph_Test(Mapping_Test):
    "Run same tests on mapping.Graph class"

    def setUp(self):
        self.datagraph = mapping.Graph()

class Graph_DB_Test(unittest.TestCase):
    "test mapping.Graph with sourceDB, targetDB but no edgeDB"

    def setUp(self):
        class Node(object):
            def __init__(self, id):
                self.id = id
        self.nodes = {1:Node(1), 2:Node(2)}
        self.datagraph = mapping.Graph(sourceDB=self.nodes,
                                       targetDB=self.nodes)
    def test_no_edge_db(self):
        'test behavior with no edgeDB'
        self.datagraph += self.nodes[1] # add node
        self.datagraph[self.nodes[1]][self.nodes[2]] = 3 # add edge

        assert self.datagraph[self.nodes[1]][self.nodes[2]] == 3
        

class GraphShelve_Test(Mapping_Test):
    "Run same tests on mapping.Graph class"

    def setUp(self):
        
        tmp = testutil.TempDir('graphshelve-test')
        filename = tmp.subfile() # needs a random name each time
        self.datagraph = mapping.Graph(filename=filename, intKeys=True)
        
    def tearDown(self):
        self.datagraph.close()

 
class SQLGraph_Test(Mapping_Test):
    "Runs the same tests on mapping.SQLGraph class"
    dbname = 'test.dumbo_foo_test'

    def setUp(self):
        if not testutil.mysql_enabled():
            raise SkipTest, "no MySQL"
    
        createOpts = dict(source_id='int', target_id='int', edge_id='int')
        self.datagraph = sqlgraph.SQLGraph(self.dbname, dropIfExists=True,
                                           createTable=createOpts)
    
    def tearDown(self):
        self.datagraph.cursor.execute('drop table if exists %s' % self.dbname)

class SQLiteGraph_Test(testutil.SQLite_Mixin, Mapping_Test):
    'run same tests on mapping.SQLGraph class using sqlite'
    def sqlite_load(self):
        createOpts = dict(source_id='int', target_id='int', edge_id='int')
        self.datagraph = sqlgraph.SQLGraph('testgraph',
                                           serverInfo=self.serverInfo,
                                           dropIfExists=True,
                                           createTable=createOpts)

# test currently unused, requires access to leelab data
## from pygr import worldbase
## class Splicegraph_Test(unittest.TestCase):
    
##     def setUp(self):
##         self.sg = worldbase.Bio.Annotation.ASAP2.Isoform.HUMAN.\
##                   hg17.splicegraph()
    
##     def exonskip_megatest(self):
##         'perform exon skip query'
##         query = {0:{1:None,2:None},1:{2:None},2:{}}
##         gq = graphquery.GraphQuery(self.sg, query)
##         l = list(gq)
##         assert len(l) == 11546, 'test exact size of exonskip set'

if __name__ == '__main__':
    PygrTestProgram(verbosity=2)
