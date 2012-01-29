#
# Copyright CEA/DAM/DIF (2007, 2008, 2009, 2010, 2011, 2012)
#  Contributor: Stephane THIELL <stephane.thiell@cea.fr>
#
# This file is part of the ClusterShell library.
#
# This software is governed by the CeCILL-C license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL-C
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL-C license and that you accept its terms.
#
# $Id$

"""
Cluster node set module.

A module to deal efficiently with 1D rangesets and nodesets (pdsh-like).
Instances of RangeSet and NodeSet both provide similar operations than
the builtin set() type and Set object.
See http://www.python.org/doc/lib/set-objects.html

Usage example
=============
  >>> # Import NodeSet class
  ... from ClusterShell.NodeSet import NodeSet
  >>>
  >>> # Create a new nodeset from string
  ... nodeset = NodeSet("cluster[1-30]")
  >>> # Add cluster32 to nodeset
  ... nodeset.update("cluster32")
  >>> # Remove from nodeset
  ... nodeset.difference_update("cluster[2-5]")
  >>> # Print nodeset as a pdsh-like pattern
  ... print nodeset
  cluster[1,6-30,32]
  >>> # Iterate over node names in nodeset
  ... for node in nodeset:
  ...     print node
  [...]
"""

from itertools import imap
from operator import itemgetter
import re
import sys

import ClusterShell.NodeUtils as NodeUtils
from ClusterShell.RangeSet import *


# Define default GroupResolver object used by NodeSet
DEF_GROUPS_CONFIG = "/etc/clustershell/groups.conf"
DEF_STD_GROUP_RESOLVER = NodeUtils.GroupResolverConfig(DEF_GROUPS_CONFIG)
STD_GROUP_RESOLVER = DEF_STD_GROUP_RESOLVER


class NodeSetException(Exception):
    """Base NodeSet exception class."""

class NodeSetParseError(NodeSetException):
    """Raised when NodeSet parsing cannot be done properly."""
    def __init__(self, part, msg):
        if part:
            msg = "%s : \"%s\"" % (msg, part)
        NodeSetException.__init__(self, msg)
        # faulty part; this allows you to target the error
        self.part = part

class NodeSetParseRangeError(NodeSetParseError):
    """Raised when bad range is encountered during NodeSet parsing."""
    def __init__(self, rset_exc):
        NodeSetParseError.__init__(self, str(rset_exc), "bad range")

class NodeSetExternalError(NodeSetException):
    """Raised when an external error is encountered."""


class NodeSetBase(object):
    """
    Base class for NodeSet.
    """
    def __init__(self, pattern=None, rangeset=None):
        """
        Initialize an empty NodeSetBase.
        """
        self._length = 0
        self._patterns = {}
        if pattern:
            self._add(pattern, rangeset)
        elif rangeset:
            raise ValueError("missing pattern")

    def _iter(self):
        """
        Iterator on internal item tuples (pattern, index, padding).
        """
        for pat, rangeset in sorted(self._patterns.iteritems()):
            if rangeset:
                pad = rangeset.padding or 0
                for idx in rangeset._sorted():
                    yield pat, idx, pad
            else:
                yield pat, None, None

    def _iterbase(self):
        """
        Iterator on single, one-item NodeSetBase objects.
        """
        for pat, start, pad in self._iter():
            if start is not None:
                yield NodeSetBase(pat, RangeSet.fromone(start, pad))
            else:
                yield NodeSetBase(pat) # no node index

    def __iter__(self):
        """
        Iterator on single nodes as string.
        """
        # Does not call self._iterbase() + str() for better performance.
        for pat, start, pad in self._iter():
            if start is not None:
                yield pat % ("%0*d" % (pad, start))
            else:
                yield pat

    # define striter() alias for convenience (to match RangeSet.striter())
    striter = __iter__

    def __len__(self):
        """
        Get the number of nodes in NodeSet.
        """
        cnt = 0
        for  rangeset in self._patterns.itervalues():
            if rangeset:
                cnt += len(rangeset)
            else:
                cnt += 1
        return cnt

    def __str__(self):
        """
        Get ranges-based pattern of node list.
        """
        result = ""
        for pat, rangeset in sorted(self._patterns.iteritems()):
            if rangeset:
                s = str(rangeset)
                cnt = len(rangeset)
                if cnt > 1:
                    s = "[" + s + "]"
                result += pat % s
            else:
                result += pat
            result += ","
        return result[:-1]

    def copy(self):
        """Return a copy of a NodeSet."""
        result = self.__class__()
        result._length = self._length
        result._autostep = self._autostep
        dic = {}
        for pat, rangeset in self._patterns.iteritems():
            if rangeset is None:
                dic[pat] = None
            else:
                dic[pat] = rangeset.copy()
        result._patterns = dic
        result._resolver = self._resolver
        result._parser = self._parser
        return result

    __copy__ = copy # For the copy module

    def __contains__(self, other):
        """
        Is node contained in NodeSet ?
        """
        return self.issuperset(other)

    def _binary_sanity_check(self, other):
        # check that the other argument to a binary operation is also
        # a NodeSet, raising a TypeError otherwise.
        if not isinstance(other, NodeSetBase):
            raise TypeError, \
                "Binary operation only permitted between NodeSetBase"

    def issubset(self, other):
        """
        Report whether another nodeset contains this nodeset.
        """
        self._binary_sanity_check(other)
        return other.issuperset(self)

    def issuperset(self, other):
        """
        Report whether this nodeset contains another nodeset.
        """
        self._binary_sanity_check(other)
        status = True
        for pat, erangeset in other._patterns.iteritems():
            rangeset = self._patterns.get(pat)
            if rangeset:
                status = rangeset.issuperset(erangeset)
            else:
                # might be an unnumbered node (key in dict but no value)
                status = self._patterns.has_key(pat)
            if not status:
                break
        return status

    def __eq__(self, other):
        """
        NodeSet equality comparison.
        """
        # See comment for for RangeSet.__eq__()
        if not isinstance(other, NodeSetBase):
            return NotImplemented
        return len(self) == len(other) and self.issuperset(other)

    # inequality comparisons using the is-subset relation
    __le__ = issubset
    __ge__ = issuperset

    def __lt__(self, other):
        """
        x.__lt__(y) <==> x<y
        """
        self._binary_sanity_check(other)
        return len(self) < len(other) and self.issubset(other)

    def __gt__(self, other):
        """
        x.__gt__(y) <==> x>y
        """
        self._binary_sanity_check(other)
        return len(self) > len(other) and self.issuperset(other)

    def _extractslice(self, index):
        """RangeSet/NodeSet private utility function: extract slice parameters
        from slice object `index` for an list-like object of size `length`."""
        length = len(self)
        if index.start is None:
            sl_start = 0
        elif index.start < 0:
            sl_start = max(0, length + index.start)
        else:
            sl_start = index.start
        if index.stop is None:
            sl_stop = sys.maxint
        elif index.stop < 0:
            sl_stop = max(0, length + index.stop)
        else:
            sl_stop = index.stop
        if index.step is None:
            sl_step = 1
        elif index.step < 0:
            # We support negative step slicing with no start/stop, ie. r[::-n].
            if index.start is not None or index.stop is not None:
                raise IndexError, \
                    "illegal start and stop when negative step is used"
            # As RangeSet elements are ordered internally, adjust sl_start
            # to fake backward stepping in case of negative slice step.
            stepmod = (length + -index.step - 1) % -index.step
            if stepmod > 0:
                sl_start += stepmod
            sl_step = -index.step
        else:
            sl_step = index.step
        if not isinstance(sl_start, int) or not isinstance(sl_stop, int) \
            or not isinstance(sl_step, int):
            raise TypeError, "slice indices must be integers"
        return sl_start, sl_stop, sl_step

    def __getitem__(self, index):
        """
        Return the node at specified index or a subnodeset when a slice is
        specified.
        """
        if isinstance(index, slice):
            inst = NodeSetBase()
            sl_start, sl_stop, sl_step = self._extractslice(self, index)
            sl_next = sl_start
            if sl_stop <= sl_next:
                return inst
            length = 0
            for pat, rangeset in sorted(self._patterns.iteritems()):
                if rangeset:
                    cnt = len(rangeset)
                    offset = sl_next - length
                    if offset < cnt:
                        num = min(sl_stop - sl_next, cnt - offset)
                        inst._add(pat, rangeset[offset:offset + num:sl_step])
                    else:
                        #skip until sl_next is reached
                        length += cnt
                        continue
                else:
                    cnt = num = 1
                    if sl_next > length:
                        length += cnt
                        continue
                    inst._add(pat, None)
                # adjust sl_next...
                sl_next += num
                if (sl_next - sl_start) % sl_step:
                    sl_next = sl_start + \
                        ((sl_next - sl_start)/sl_step + 1) * sl_step
                if sl_next >= sl_stop:
                    break
                length += cnt
            return inst
        elif isinstance(index, int):
            if index < 0:
                length = len(self)
                if index >= -length:
                    index = length + index # - -index
                else:
                    raise IndexError, "%d out of range" % index
            length = 0
            for pat, rangeset in sorted(self._patterns.iteritems()):
                if rangeset:
                    cnt = len(rangeset)
                    if index < length + cnt:
                        # return a subrangeset of size 1 to manage padding
                        return pat % rangeset[index - length:index - length + 1]
                else:
                    cnt = 1
                    if index == length:
                        return pat
                length += cnt
            raise IndexError, "%d out of range" % index
        else:
            raise TypeError, "NodeSet indices must be integers"

    def _add(self, pat, rangeset):
        """
        Add nodes from a (pat, rangeset) tuple. `pat' may be an existing
        pattern and `rangeset' may be None.
        """
        # get patterns dict entry
        pat_e = self._patterns.get(pat)

        if pat_e:
            # don't play with prefix: if there is a value, there is a
            # rangeset.
            assert rangeset != None

            # add rangeset in corresponding pattern rangeset
            pat_e.update(rangeset)
        elif rangeset:
            # create new pattern
            self._patterns[pat] = rangeset.copy()
        else:
            # create new pattern with no rangeset (single node)
            self._patterns[pat] = None

    def _addn(self, pat, rangesets):
        """
        Add nodes from a (pat, list of rangesets).
        """
        assert len(rangesets) > 0
        # get patterns dict entry
        pat_e = self._patterns.get(pat)
        
        # check for single node presence
        single = True
        for rng in rangesets:
            if rng is not None:
                single = False
                break

        if pat_e is None:
            if single:
                self._patterns[pat] = None
            else:
                pat_e = self._patterns[pat] = rangesets[0].copy()
                pat_e.updaten(rangesets[1:])
        else:
            assert not single
            pat_e.updaten(rangesets)

    def union(self, other):
        """
        s.union(t) returns a new set with elements from both s and t.
        """
        self_copy = self.copy()
        self_copy.update(other)
        return self_copy

    def __or__(self, other):
        """
        Implements the | operator. So s | t returns a new nodeset with
        elements from both s and t.
        """
        if not isinstance(other, NodeSetBase):
            return NotImplemented
        return self.union(other)

    def add(self, other):
        """
        Add node to NodeSet.
        """
        self.update(other)

    def update(self, other):
        """
        s.update(t) returns nodeset s with elements added from t.
        """
        self._binary_sanity_check(other)

        for pat, rangeset in other._patterns.iteritems():
            self._add(pat, rangeset)

    def updaten(self, others):
        """
        s.updaten(t) returns nodeset s with elements added from given list.
        """
        # optimized for pattern homogeneous clusters (a common case)
        patd = {}
        # gather rangesets from each common nodeset pattern
        for other in others:
            self._binary_sanity_check(other)
            for pat, rangeset in other._patterns.iteritems():
                patd.setdefault(pat, []).append(rangeset)
        # for each pattern, add all needed rangesets in once
        for pat, rgsets in patd.iteritems():
            self._addn(pat, rgsets)
        
    def clear(self):
        """
        Remove all nodes from this nodeset.
        """
        self._patterns.clear()

    def __ior__(self, other):
        """
        Implements the |= operator. So s |= t returns nodeset s with
        elements added from t. (Python version 2.5+ required)
        """
        self._binary_sanity_check(other)
        self.update(other)
        return self

    def intersection(self, other):
        """
        s.intersection(t) returns a new set with elements common to s
        and t.
        """
        self_copy = self.copy()
        self_copy.intersection_update(other)
        return self_copy

    def __and__(self, other):
        """
        Implements the & operator. So s & t returns a new nodeset with
        elements common to s and t.
        """
        if not isinstance(other, NodeSet):
            return NotImplemented
        return self.intersection(other)

    def intersection_update(self, other):
        """
        s.intersection_update(t) returns nodeset s keeping only
        elements also found in t.
        """
        self._binary_sanity_check(other)

        if other is self:
            return

        tmp_ns = NodeSetBase()

        for pat, irangeset in other._patterns.iteritems():
            rangeset = self._patterns.get(pat)
            if rangeset:
                rs = rangeset.copy()
                rs.intersection_update(irangeset)
                # ignore pattern if empty rangeset
                if len(rs) > 0:
                    tmp_ns._add(pat, rs)
            elif not irangeset and pat in self._patterns:
                # intersect two nodes with no rangeset
                tmp_ns._add(pat, None)
            elif not irangeset and pat in self._patterns:
                # intersect two nodes with no rangeset
                tmp_ns._add(pat, None)

        # Substitute 
        self._patterns = tmp_ns._patterns

    def __iand__(self, other):
        """
        Implements the &= operator. So s &= t returns nodeset s keeping
        only elements also found in t. (Python version 2.5+ required)
        """
        self._binary_sanity_check(other)
        self.intersection_update(other)
        return self

    def difference(self, other):
        """
        s.difference(t) returns a new NodeSet with elements in s but not
        in t.
        """
        self_copy = self.copy()
        self_copy.difference_update(other)
        return self_copy

    def __sub__(self, other):
        """
        Implement the - operator. So s - t returns a new nodeset with
        elements in s but not in t.
        """
        if not isinstance(other, NodeSetBase):
            return NotImplemented
        return self.difference(other)

    def difference_update(self, other, strict=False):
        """
        s.difference_update(t) returns nodeset s after removing
        elements found in t. If strict is True, raise KeyError
        if an element cannot be removed.
        """
        self._binary_sanity_check(other)
        # the purge of each empty pattern is done afterward to allow self = ns
        purge_patterns = []

        # iterate first over exclude nodeset rangesets which is usually smaller
        for pat, erangeset in other._patterns.iteritems():
            # if pattern is found, deal with it
            rangeset = self._patterns.get(pat)
            if rangeset:
                # sub rangeset, raise KeyError if not found
                rangeset.difference_update(erangeset, strict)

                # check if no range left and add pattern to purge list
                if len(rangeset) == 0:
                    purge_patterns.append(pat)
            else:
                # unnumbered node exclusion
                if self._patterns.has_key(pat):
                    purge_patterns.append(pat)
                elif strict:
                    raise KeyError, pat

        for pat in purge_patterns:
            del self._patterns[pat]

    def __isub__(self, other):
        """
        Implement the -= operator. So s -= t returns nodeset s after
        removing elements found in t. (Python version 2.5+ required)
        """
        self._binary_sanity_check(other)
        self.difference_update(other)
        return self

    def remove(self, elem):
        """
        Remove element elem from the nodeset. Raise KeyError if elem
        is not contained in the nodeset.
        """
        self.difference_update(elem, True)

    def symmetric_difference(self, other):
        """
        s.symmetric_difference(t) returns the symmetric difference of
        two nodesets as a new NodeSet.
        
        (ie. all nodes that are in exactly one of the nodesets.)
        """
        self_copy = self.copy()
        self_copy.symmetric_difference_update(other)
        return self_copy

    def __xor__(self, other):
        """
        Implement the ^ operator. So s ^ t returns a new NodeSet with
        nodes that are in exactly one of the nodesets.
        """
        if not isinstance(other, NodeSet):
            return NotImplemented
        return self.symmetric_difference(other)

    def symmetric_difference_update(self, other):
        """
        s.symmetric_difference_update(t) returns nodeset s keeping all
        nodes that are in exactly one of the nodesets.
        """
        self._binary_sanity_check(other)
        purge_patterns = []

        # iterate over our rangesets
        for pat, rangeset in self._patterns.iteritems():
            brangeset = other._patterns.get(pat)
            if brangeset:
                rangeset.symmetric_difference_update(brangeset)
            else:
                if other._patterns.has_key(pat):
                    purge_patterns.append(pat)

        # iterate over other's rangesets
        for pat, brangeset in other._patterns.iteritems():
            rangeset = self._patterns.get(pat)
            if not rangeset and not self._patterns.has_key(pat):
                self._add(pat, brangeset)

        # check for patterns cleanup
        for pat, rangeset in self._patterns.iteritems():
            if rangeset is not None and len(rangeset) == 0:
                purge_patterns.append(pat)

        # cleanup
        for pat in purge_patterns:
            del self._patterns[pat]

    def __ixor__(self, other):
        """
        Implement the ^= operator. So s ^= t returns nodeset s after
        keeping all nodes that are in exactly one of the nodesets.
        (Python version 2.5+ required)
        """
        self._binary_sanity_check(other)
        self.symmetric_difference_update(other)
        return self


class NodeGroupBase(NodeSetBase):
    """
    """
    def _add(self, pat, rangeset):
        """
        Add groups from a (pat, rangeset) tuple. `pat' may be an existing
        pattern and `rangeset' may be None.
        """
        if pat and pat[0] != '@':
            raise ValueError("NodeGroup name must begin with character '@'")
        NodeSetBase._add(self, pat, rangeset)


class ParsingEngine(object):
    """
    Class that is able to transform a source into a NodeSetBase.
    """
    OP_CODES = { 'update': ',',
                 'difference_update': '!',
                 'intersection_update': '&',
                 'symmetric_difference_update': '^' }

    def __init__(self, group_resolver):
        """
        Initialize Parsing Engine.
        """
        self.group_resolver = group_resolver
        self.single_node_re = re.compile("(\D*)(\d*)(.*)")

    def parse(self, nsobj, autostep):
        """
        Parse provided object if possible and return a NodeSetBase object.
        """
        # passing None is supported
        if nsobj is None:
            return NodeSetBase()

        # is nsobj a NodeSetBase instance?
        if isinstance(nsobj, NodeSetBase):
            return nsobj

        # or is nsobj a string?
        if type(nsobj) is str:
            try:
                return self.parse_string(str(nsobj), autostep)
            except NodeUtils.GroupSourceQueryFailed, exc:
                raise NodeSetParseError(nsobj, str(exc))

        raise TypeError("Unsupported NodeSet input %s" % type(nsobj))
        
    def parse_string(self, nsstr, autostep):
        """
        Parse provided string and return a NodeSetBase object.
        """
        nodeset = NodeSetBase()

        for opc, pat, rangeset in self._scan_string(nsstr, autostep):
            # Parser main debugging:
            #print "OPC %s PAT %s RANGESET %s" % (opc, pat, rangeset)
            if self.group_resolver and pat[0] == '@':
                ns_group = NodeSetBase()
                for nodegroup in NodeGroupBase(pat, rangeset):
                    # parse/expand nodes group
                    ns_string_ext = self.parse_group_string(nodegroup)
                    if ns_string_ext:
                        # convert result and apply operation
                        ns_group.update(self.parse(ns_string_ext, autostep))
                # perform operation
                getattr(nodeset, opc)(ns_group)
            else:
                getattr(nodeset, opc)(NodeSetBase(pat, rangeset))

        return nodeset
        
    def parse_group(self, group, namespace=None, autostep=None):
        """Parse provided single group name (without @ prefix)."""
        assert self.group_resolver is not None
        nodestr = self.group_resolver.group_nodes(group, namespace)
        return self.parse(",".join(nodestr), autostep)
        
    def parse_group_string(self, nodegroup):
        """Parse provided group string and return a string."""
        assert nodegroup[0] == '@'
        assert self.group_resolver is not None
        grpstr = nodegroup[1:]
        if grpstr.find(':') < 0:
            # default namespace
            return ",".join(self.group_resolver.group_nodes(grpstr))
        else:
            # specified namespace
            namespace, group = grpstr.split(':', 1)
            return ",".join(self.group_resolver.group_nodes(group, namespace))

    def _next_op(self, pat):
        """Opcode parsing subroutine."""
        op_idx = -1
        next_op_code = None
        for opc, idx in [(k, pat.find(v)) \
                            for k, v in ParsingEngine.OP_CODES.iteritems()]:
            if idx >= 0 and (op_idx < 0 or idx <= op_idx):
                next_op_code = opc
                op_idx = idx
        return op_idx, next_op_code

    def _scan_string(self, nsstr, autostep):
        """
        Parsing engine's string scanner method.
        """
        pat = nsstr.strip()
        # avoid misformatting
        if pat.find('%') >= 0:
            pat = pat.replace('%', '%%')
        next_op_code = 'update'
        while pat is not None:
            # Ignore whitespace(s) for convenience
            pat = pat.lstrip()

            op_code, next_op_code = next_op_code, None
            op_idx = -1
            op_idx, next_op_code = self._next_op(pat)
            bracket_idx = pat.find('[')

            # Check if the operator is after the bracket, or if there
            # is no operator at all but some brackets.
            if bracket_idx >= 0 and (op_idx > bracket_idx or op_idx < 0):
                # In this case, we have a pattern of potentially several
                # nodes.
                # Fill prefix, range and suffix from pattern
                # eg. "forbin[3,4-10]-ilo" -> "forbin", "3,4-10", "-ilo"
                pfx, sfx = pat.split('[', 1)
                try:
                    rng, sfx = sfx.split(']', 1)
                except ValueError:
                    raise NodeSetParseError(pat, "missing bracket")

                # Check if we have a next op-separated node or pattern
                op_idx, next_op_code = self._next_op(sfx)
                if op_idx < 0:
                    pat = None
                else:
                    sfx, pat = sfx.split(self.OP_CODES[next_op_code], 1)

                # Ignore whitespace(s)
                sfx = sfx.rstrip()

                # pfx + sfx cannot be empty
                if len(pfx) + len(sfx) == 0:
                    raise NodeSetParseError(pat, "empty node name")

                # Process comma-separated ranges
                try:
                    rset = RangeSet(rng, autostep)
                except RangeSetParseError, e:
                    raise NodeSetParseRangeError(e)

                yield op_code, "%s%%s%s" % (pfx, sfx), rset
            else:
                # In this case, either there is no comma and no bracket,
                # or the bracket is after the comma, then just return
                # the node.
                if op_idx < 0:
                    node = pat
                    pat = None # break next time
                else:
                    node, pat = pat.split(self.OP_CODES[next_op_code], 1)
                # Ignore whitespace(s)
                node = node.strip()

                if len(node) == 0:
                    raise NodeSetParseError(pat, "empty node name")

                # single node parsing
                mo = self.single_node_re.match(node)
                if not mo:
                    raise NodeSetParseError(pat, "parse error")
                pfx, idx, sfx = mo.groups()
                pfx, sfx = pfx or "", sfx or ""

                # pfx+sfx cannot be empty
                if len(pfx) + len(sfx) == 0:
                    raise NodeSetParseError(pat, "empty node name")

                if idx:
                    try:
                        rset = RangeSet(idx, autostep)
                    except RangeSetParseError, e:
                        raise NodeSetParseRangeError(e)
                    p = "%s%%s%s" % (pfx, sfx)
                    yield op_code, p, rset
                else:
                    # undefined pad means no node index
                    yield op_code, pfx, None


# Special constant for NodeSet's resolver parameter to avoid any group
# resolution at all.
NOGROUP_RESOLVER = -1


class NodeSet(NodeSetBase):
    """
    Iterable class of nodes with node ranges support.

    NodeSet creation examples:
       >>> nodeset = NodeSet()               # empty NodeSet
       >>> nodeset = NodeSet("cluster3")     # contains only cluster3
       >>> nodeset = NodeSet("cluster[5,10-42]")
       >>> nodeset = NodeSet("cluster[0-10/2]")
       >>> nodeset = NodeSet("cluster[0-10/2],othername[7-9,120-300]")

    NodeSet provides methods like update(), intersection_update() or
    difference_update() methods, which conform to the Python Set API.
    However, unlike RangeSet or standard Set, NodeSet is somewhat not
    so strict for convenience, and understands NodeSet instance or
    NodeSet string as argument. Also, there is no strict definition of
    one element, for example, it IS allowed to do:
        >>> nodeset.remove("blue[36-40]")

    Additionally, the NodeSet class recognizes the "extended string
    pattern" which adds support for union (special character ","),
    difference ("!"), intersection ("&") and symmetric difference ("^")
    operations. String patterns are read from left to right, by
    proceeding any character operators accordinately.

    Extended string pattern usage examples:
        >>> nodeset = NodeSet("node[0-10],node[14-16]") # union
        >>> nodeset = NodeSet("node[0-10]!node[8-10]")  # difference
        >>> nodeset = NodeSet("node[0-10]&node[5-13]")  # intersection
        >>> nodeset = NodeSet("node[0-10]^node[5-13]")  # xor
    """
    def __init__(self, nodes=None, autostep=None, resolver=None):
        """
        Initialize a NodeSet.
        The `nodes' argument may be a valid nodeset string or a NodeSet
        object. If no nodes are specified, an empty NodeSet is created.
        """
        NodeSetBase.__init__(self)

        self._autostep = autostep

        # Set group resolver.
        self._resolver = None
        if resolver != NOGROUP_RESOLVER:
            self._resolver = resolver or STD_GROUP_RESOLVER

        # Initialize default parser.
        self._parser = ParsingEngine(self._resolver)

        self.update(nodes)

    @classmethod
    def fromlist(cls, nodelist, autostep=None, resolver=None):
        """
        Class method that returns a new NodeSet with nodes from
        provided list.
        """
        inst = NodeSet(autostep=autostep, resolver=resolver)
        inst.updaten(nodelist)
        return inst

    @classmethod
    def fromall(cls, groupsource=None, autostep=None, resolver=None):
        """
        Class method that returns a new NodeSet with all nodes from
        optional groupsource.
        """
        inst = NodeSet(autostep=autostep, resolver=resolver)
        if not inst._resolver:
            raise NodeSetExternalError("No node group resolver")
        try:
            # Ask resolver to provide all nodes.
            for nodes in inst._resolver.all_nodes(groupsource):
                inst.update(nodes)
        except NodeUtils.GroupSourceNoUpcall:
            # As the resolver is not able to provide all nodes directly,
            # failback to list + map(s) method:
            try:
                # Like in regroup(), we get a NodeSet of all groups in
                # specified group source.
                allgrpns = NodeSet.fromlist( \
                                inst._resolver.grouplist(groupsource),
                                resolver=NOGROUP_RESOLVER)
                # For each individual group, resolve it to node and accumulate.
                for grp in allgrpns:
                    inst.update(NodeSet.fromlist( \
                                inst._resolver.group_nodes(grp, groupsource)))
            except NodeUtils.GroupSourceNoUpcall:
                # We are not able to find "all" nodes, definitely.
                raise NodeSetExternalError("Not enough working external " \
                    "calls (all, or map + list) defined to get all nodes")
        except NodeUtils.GroupSourceQueryFailed, exc:
            raise NodeSetExternalError("Unable to get all nodes due to the " \
                "following external failure:\n\t%s" % exc)
        return inst

    def __getstate__(self):
        """Called when pickling: remove references to group resolver."""
        odict = self.__dict__.copy()
        del odict['_resolver']
        del odict['_parser']
        return odict

    def __setstate__(self, dic):
        """Called when unpickling: restore parser using non group
        resolver."""
        self.__dict__.update(dic)
        self._resolver = None
        self._parser = ParsingEngine(None)

    def _find_groups(self, node, namespace, allgroups):
        """Find groups of node by namespace."""
        if allgroups:
            # find node groups using in-memory allgroups
            for grp, nodeset in allgroups.iteritems():
                if node in nodeset:
                    yield grp
        else:
            # find node groups using resolver
            for group in self._resolver.node_groups(node, namespace):
                yield group

    def regroup(self, groupsource=None, autostep=None, overlap=False,
                noprefix=False):
        """
        Regroup nodeset using groups.
        """
        groups = {}
        rest = NodeSet(self, resolver=NOGROUP_RESOLVER)

        try:
            # Get a NodeSet of all groups in specified group source.
            allgrpns = NodeSet.fromlist(self._resolver.grouplist(groupsource),
                                        resolver=NOGROUP_RESOLVER)
        except NodeUtils.GroupSourceException:
            # If list query failed, we still might be able to regroup
            # using reverse.
            allgrpns = None

        allgroups = {}

        # Check for external reverse presence, and also use the
        # following heuristic: external reverse is used only when number
        # of groups is greater than the NodeSet size.
        if self._resolver.has_node_groups(groupsource) and \
            (not allgrpns or len(allgrpns) >= len(self)):
            # use external reverse
            pass
        else:
            if not allgrpns: # list query failed and no way to reverse!
                return str(rest)
            try:
                # use internal reverse: populate allgroups
                for grp in allgrpns:
                    nodelist = self._resolver.group_nodes(grp, groupsource)
                    allgroups[grp] = NodeSet(",".join(nodelist))
            except NodeUtils.GroupSourceQueryFailed, exc:
                # External result inconsistency
                raise NodeSetExternalError("Unable to map a group " \
                        "previously listed\n\tFailed command: %s" % exc)

        # For each NodeSetBase in self, finds its groups.
        for node in self._iterbase():
            for grp in self._find_groups(node, groupsource, allgroups):
                if grp not in groups:
                    nodes = self._parser.parse_group(grp, groupsource, autostep)
                    groups[grp] = (0, nodes)
                i, nodes = groups[grp]
                groups[grp] = (i + 1, nodes)
                
        # Keep only groups that are full.
        fulls = []
        for k, (i, nodes) in groups.iteritems():
            assert i <= len(nodes)
            if i == len(nodes):
                fulls.append((i, k))

        regrouped = NodeSet(resolver=NOGROUP_RESOLVER)

        bigalpha = lambda x, y: cmp(y[0], x[0]) or cmp(x[1], y[1])

        # Build regrouped NodeSet by selecting largest groups first.
        for num, grp in sorted(fulls, cmp=bigalpha):
            if not overlap and groups[grp][1] not in rest:
                continue
            if groupsource and not noprefix:
                regrouped.update("@%s:%s" % (groupsource, grp))
            else:
                regrouped.update("@" + grp)
            rest.difference_update(groups[grp][1])
            if not rest:
                return str(regrouped)

        if regrouped:
            return "%s,%s" % (regrouped, rest)

        return str(rest)

    def issubset(self, other):
        """
        Report whether another nodeset contains this nodeset.
        """
        nodeset = self._parser.parse(other, self._autostep)
        return NodeSetBase.issuperset(nodeset, self)

    def issuperset(self, other):
        """
        Report whether this nodeset contains another nodeset.
        """
        nodeset = self._parser.parse(other, self._autostep)
        return NodeSetBase.issuperset(self, nodeset)

    def __getitem__(self, index):
        """
        Return the node at specified index or a subnodeset when a slice
        is specified.
        """
        base = NodeSetBase.__getitem__(self, index)
        if not isinstance(base, NodeSetBase):
            return base
        # return a real NodeSet
        inst = NodeSet(autostep=self._autostep, resolver=self._resolver)
        inst._patterns = base._patterns
        return inst

    def split(self, nbr):
        """
        Split the nodeset into nbr sub-nodesets (at most). Each
        sub-nodeset will have the same number of elements more or
        less 1. Current nodeset remains unmodified.

        >>> NodeSet("foo[1-5]").split(3) 
        NodeSet("foo[1-2]")
        NodeSet("foo[3-4]")
        NodeSet("foo5")
        """
        assert(nbr > 0)

        # We put the same number of element in each sub-nodeset.
        slice_size = len(self) / nbr
        left = len(self) % nbr

        begin = 0
        for i in range(0, min(nbr, len(self))):
            length = slice_size + int(i < left)
            yield self[begin:begin + length]
            begin += length

    def update(self, other):
        """
        s.update(t) returns nodeset s with elements added from t.
        """
        nodeset = self._parser.parse(other, self._autostep)
        NodeSetBase.update(self, nodeset)

    def updaten(self, others):
        """
        s.updaten(list) returns nodeset s with elements added from given list.
        """
        NodeSetBase.updaten(self, \
            [self._parser.parse(other, self._autostep) for other in others])

    def intersection_update(self, other):
        """
        s.intersection_update(t) returns nodeset s keeping only
        elements also found in t.
        """
        nodeset = self._parser.parse(other, self._autostep)
        NodeSetBase.intersection_update(self, nodeset)

    def difference_update(self, other, strict=False):
        """
        s.difference_update(t) returns nodeset s after removing
        elements found in t. If strict is True, raise KeyError
        if an element cannot be removed.
        """
        nodeset = self._parser.parse(other, self._autostep)
        NodeSetBase.difference_update(self, nodeset, strict)

    def symmetric_difference_update(self, other):
        """
        s.symmetric_difference_update(t) returns nodeset s keeping all
        nodes that are in exactly one of the nodesets.
        """
        nodeset = self._parser.parse(other, self._autostep)
        NodeSetBase.symmetric_difference_update(self, nodeset)


def expand(pat):
    """
    Commodity function that expands a pdsh-like pattern into a list of
    nodes.
    """
    return list(NodeSet(pat))

def fold(pat):
    """
    Commodity function that clean dups and fold provided pattern with
    ranges and "/step" support.
    """
    return str(NodeSet(pat))

def grouplist(namespace=None):
    """
    Commodity function that retrieves the list of groups for a specified
    group namespace (or use default namespace).
    """
    return STD_GROUP_RESOLVER.grouplist(namespace)


# doctest

def _test():
    import doctest
    doctest.testmod()

if __name__ == '__main__':
    _test()

