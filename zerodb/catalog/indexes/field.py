import itertools as it

from repoze.catalog.indexes.field import CatalogFieldIndex as _CatalogFieldIndex
from repoze.catalog.indexes.common import *
from repoze.catalog import RangeValue
from zerodb import trees
from zerodb.catalog.indexes.common import CallableDiscriminatorMixin
from zerodb.storage import prefetch
from zerodb.util.iter import ListPrefetch

import BTrees

BTreeSet = BTrees.family32.IF.TreeSet

_marker = ()

threshold = 10

def multiunion1(set_type, seqs):
    # XXX simple/slow implementation. Goal is just to get tests to pass.
    result = set_type()
    for s in seqs:
        try:
            iter(s)
        except TypeError:
            s = set_type((s, ))
        result.update(s)
    return result

class NewTreeItems(object):
    def __init__(self, items):
        self.items = items
        self.iterator = iter(items)

    def __getitem__(self, i):
        v = self.items.__getitem__(i)
        if isinstance(v, int):
            return (v,)
        else:
            return v

    def __len__(self):
        return self.items.__len__()

    def __iter__(self):
        for nextobj in self.iterator:
            if isinstance(nextobj, int):
                yield (nextobj,)
            else:
                yield nextobj

class CatalogFieldIndex(CallableDiscriminatorMixin, _CatalogFieldIndex):
    family = trees.family32

    # TODO prefetch in search method before multiunion
    # Pass data through apply method

    def __init__(self, discriminator):
        self._init_discriminator(discriminator)
        self._not_indexed = self.family.IF.Set()
        self.clear()

    def applyInRange(self, start, end, excludemin=False, excludemax=False):
        return ListPrefetch(lambda: it.chain.from_iterable(
            ListPrefetch(lambda: NewTreeItems(self._fwd_index.values(start, end, excludemin=excludemin, excludemax=excludemax)))))
        # XXX what if these treesets are pretty deep? Need to pre-fetch "first N elements"

    def scan_forward(self, docids, limit=None):
        # Batch-prefetch treesets
        # If sorting index is the same as _fwd_index, we already pre-fetched
        # the same objects in the same order!
        fwd_index = ListPrefetch(lambda: self._fwd_index.values())

        n = 0
        for curdocids in fwd_index:
            if isinstance(curdocids, int) and curdocids in docids:
                n += 1
                yield curdocids
                if limit and n >= limit:
                    raise StopIteration
            elif isinstance(curdocids, tuple) or isinstance(curdocids, BTreeSet):
                for docid in curdocids:
                    if docid in docids:
                        n += 1
                        yield docid
                        if limit and n >= limit:
                            raise StopIteration

    def index_doc(self, docid, obj):
        if self.discriminator_callable:
            # Model class definition has a list of virtual fields
            virtuals = getattr(obj.__class__, "_z_virtual_fields", {})
            value = virtuals.get(self.discriminator, _marker)
            if value != _marker:
                try:
                    value = value(obj)
                except:
                    value = _marker
        else:
            value = getattr(obj, self.discriminator, _marker)

        if value is _marker:
            # unindex the previous value
            super(CatalogIndex, self).unindex_doc(docid)

            # Store docid in set of unindexed docids
            self._not_indexed.add(docid)

            return None

        if isinstance(value, Persistent):
            raise ValueError('Catalog cannot index persistent object %s' %
                             value)

        if isinstance(value, Broken):
            raise ValueError('Catalog cannot index broken object %s' %
                             value)

        if docid in self._not_indexed:
            # Remove from set of unindexed docs if it was in there.
            self._not_indexed.remove(docid)

        return self.inner_index_doc(docid, value)
        #return super(CatalogIndex, self).index_doc(docid, value)

    def inner_index_doc(self, docid, value):
        """See interface IInjection"""
        rev_index = self._rev_index
        if docid in rev_index:
            docids = self._fwd_index.get(value, ())
            if isinstance(docids, int):
                if docids == docid:
                    return
            elif docid in docids:
                # no need to index the doc, its already up to date
                return
            # unindex doc if present
            self.unindex_doc(docid)

        # Insert into forward index.
        curdocids = self._fwd_index.get(value)
        if curdocids is None:
            #set = self.family.IF.TreeSet()
            self._fwd_index[value] = docid # integer when only one docid
            #self._fwd_index[value] = set
        else:
            if isinstance(curdocids, int):
                curdocnum = 1
            else:
                curdocnum = len(curdocids)
            '''
            if curdocnum <= threshold-1 and isinstance(curdocids, BTreeSet):  # compatible with legacy TreeSet
                tmptuple = tuple()
                for docid in curdocids.keys():
                    tmptuple += docid
                curdocids = tmptuple
            '''    
            newdocids = curdocids

            if isinstance(curdocids, int):
                #self._fwd_index[value] = (curdocids, docid)
                newdocids = (curdocids,)
            elif curdocnum == threshold-1 and isinstance(curdocids, tuple):
                newset = BTreeSet()
                newset.update(curdocids)
       
                #self._fwd_index[value] = set
                newdocids = newset

            if isinstance(newdocids, tuple):
                newdocids += (docid,)
            elif isinstance(newdocids, BTreeSet):
                newdocids.insert(docid) 

            self._fwd_index[value] = newdocids

        # increment doc count
        self._num_docs.change(1)

        # Insert into reverse index.
        rev_index[docid] = value


    def search(self, queries, operator='or'):
        sets = []
        for query in queries:
            if isinstance(query, RangeValue):
                query = query.as_tuple()
            else:
                query = (query, query)
            #set = self.family.IF.multiunion(self._fwd_index.values(*query))
            set = multiunion1(BTreeSet, self._fwd_index.values(*query))
            sets.append(set)

        result = None

        if len(sets) == 1:
            result = sets[0]
        elif operator == 'and':
            sets.sort()
            for set in sets:
                result = self.family.IF.intersection(set, result)
        else:
            result = self.family.IF.multiunion(sets)

        return result

    def unindex_doc(self, docid):
        """See interface IInjection.
        Base class overridden to be able to unindex None values.
        """
        _not_indexed = self._not_indexed
        if docid in _not_indexed:
            _not_indexed.remove(docid)

        rev_index = self._rev_index
        value = rev_index.get(docid, _marker)
        if value is _marker:
            return # not in index

        del rev_index[docid]
   
        delvalue = False

        try:
            docids = self._fwd_index[value]
            if isinstance(docids, int):
                delvalue = True
            elif isinstance(docids, tuple):
                newtuple = []
                for i in docids:
                    if i != docid:
                        newtuple.append(i)
                if len(newtuple) == 0:
                    delvalue = True
                else:
                    self._fwd_index[value] = tuple(newtuple)
            elif isinstance(docids, BTreeSet):
                set = self._fwd_index[value]
                set.remove(docid)
                if not set:
                    delvalue = True
        except KeyError:
                pass

        if delvalue:
            del self._fwd_index[value]

        self._num_docs.change(-1)
