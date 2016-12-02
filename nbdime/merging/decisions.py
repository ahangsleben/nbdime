# coding: utf-8

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import unicode_literals

from six import string_types, text_type
from six.moves import xrange as range

import copy
import nbformat

from ..diff_format import (
    DiffOp, op_removerange, op_remove, op_patch, op_replace)
from ..patching import patch
from ..utils import r_is_int, star_path, join_path


class MergeDecision(dict):
    """For internal usage in nbdime library.

    Minimal class providing attribute access to merge decision keys.

    Tip: If performance dictates, we can easily replace this
    with a namedtuple during processing of diffs and convert
    to dicts before any json conversions.
    """

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            return self.__getattribute__(name)
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value

    def local_path(self):
        level = self.get(_level, 0)
        return (self.common_path or ())[level:]


class MergeDecisionBuilder(object):
    """A helper class for building a series of decisions to describe a merge.
    """
    def __init__(self):
        self.decisions = []

    def validated(self, base):
        """Returns decisions in state ready for application.

        Most importantly, this sorts the decisions on the path, so that it is
        in accordance with the specs.
        """
        return sorted(self.decisions, key=_sort_key, reverse=True)

    def add_decision(self, path, action, local_diff, remote_diff,
                     conflict=False, **kwargs):
        """Add a decision to the builder with the specified properties.

        Ensures data types and paths are as they should be, before creating a
        MergeDecision and adding it to its internal store.
        """
        # Ensure path is immutable
        if isinstance(path, list):
            path = tuple(path)
        else:
            assert isinstance(path, tuple)
        # Ensure diffs are lists
        if local_diff is not None:
            if isinstance(local_diff, tuple):
                local_diff = list(local_diff)
            elif not isinstance(local_diff, list):
                local_diff = [local_diff]
        if remote_diff is not None:
            if isinstance(remote_diff, tuple):
                remote_diff = list(remote_diff)
            elif not isinstance(remote_diff, list):
                remote_diff = [remote_diff]
        custom_diff = kwargs.pop("custom_diff", None)
        if custom_diff is not None:
            if isinstance(custom_diff, tuple):
                custom_diff = list(custom_diff)
            elif not isinstance(custom_diff, list):
                custom_diff = [custom_diff]
        # Ensure paths are pushed out as far in tree as possible
        path, (local_diff, remote_diff, custom_diff) = \
            ensure_common_path(path, [local_diff, remote_diff, custom_diff])
        if custom_diff is not None:
            kwargs["custom_diff"] = custom_diff

        # Finally store decision
        self.decisions.append(MergeDecision(
            common_path=path,
            conflict=conflict,
            action=action,
            local_diff=local_diff,
            remote_diff=remote_diff,
            **kwargs
            ))

    def keep(self, path, key, local_diff, remote_diff):
        self.add_decision(
            path=path,
            action="base",
            local_diff=local_diff,
            remote_diff=remote_diff
        )

    def onesided(self, path, local_diff, remote_diff, conflict=False):
        assert local_diff or remote_diff
        assert not (local_diff and remote_diff)
        if local_diff:
            action = "local"
        elif remote_diff:
            action = "remote"
        self.add_decision(
            path=path,
            action=action,
            local_diff=local_diff,
            remote_diff=remote_diff,
            )

    def local_then_remote(self, path, local_diff, remote_diff, conflict=False):
        assert local_diff and remote_diff
        assert local_diff != remote_diff
        action = "local_then_remote"
        self.add_decision(
            path=path,
            conflict=conflict,
            action=action,
            local_diff=local_diff,
            remote_diff=remote_diff
            )

    def remote_then_local(self, path, local_diff, remote_diff, conflict=False):
        assert local_diff and remote_diff
        assert local_diff != remote_diff
        action = "remote_then_local"
        self.add_decision(
            path=path,
            conflict=conflict,
            action=action,
            local_diff=local_diff,
            remote_diff=remote_diff
            )

    def agreement(self, path, local_diff, remote_diff, conflict=False):
        assert local_diff and remote_diff
        assert local_diff == remote_diff
        self.add_decision(
            path=path,
            action="either",
            local_diff=local_diff,
            remote_diff=remote_diff,
            )

    def conflict(self, path, local_diff, remote_diff):
        assert local_diff and remote_diff
        assert local_diff != remote_diff
        action = "base"
        self.add_decision(
            path=path,
            conflict=True,
            action=action,
            local_diff=local_diff,
            remote_diff=remote_diff,
            )


def ensure_common_path(path, diffs):
    """Resolves common paths in a list of diffs.

    If a local and a remote diff both patch a key "a", this will return the
    common path ("a",), and the inner diffs of the patch operations. Works
    recursively, so a common chain of patches will be resolved as well.
    """
    assert isinstance(path, (tuple, list))
    popped = _pop_path(diffs)
    while popped:
        path = path + (popped["key"],)
        diffs = popped["diffs"]
        popped = _pop_path(diffs)
    return path, diffs


def _pop_path(diffs):
    """Pops of a common path from patch ops sharing a common key.

    Checks whether all diffs are single patch operations sharing the same key,
    or alternatively empty diffs. If so, it returns the shared path/key as well
    as the inner diffs of the patch operations (in the same order as the passed
    diffs).
    """
    key = None
    popped_diffs = []
    for d in diffs:
        # Empty diffs can be skipped
        if d is None or len(d) == 0:
            popped_diffs.append(None)
            continue
        # Check that we have only one op, which is a patch op
        if len(d) != 1 or d[0].op != DiffOp.PATCH:
            return
        # Ensure all present diffs have the same key
        if key is None:
            key = d[0].key
        elif key != d[0].key:
            return
        # Ensure the sub diffs of all ops are suitable as outer layer
        # if d[0].diff.length > 1:
        #    return
        popped_diffs.append(d[0].diff)
    if key is None:
        return
    return {'key': key, 'diffs': popped_diffs}


def push_path(path, diffs):
    """Wraps the diffs in patch operations matching path.
    """
    for key in reversed(path):
        diffs = [op_patch(key, diffs)]
    return diffs


def pop_patch_decision(decision):
    """Create a new decision one level lower in diff tree.

    Checks whether all diffs are single patch operations sharing the same key,
    or alternatively empty diffs. A new decision is then created at that key.

    Returns the new decision.

    Returns None if a decision can not be created at the lower level.
    """
    diffs = [decision.local_diff, decision.remote_diff]
    if decision.action == "custom":
        diffs.append(decision.custom_diff)
    popped = _pop_path(diffs)
    if popped is None:
        return None
    ret = MergeDecision(
        common_path=decision.common_path + (popped["key"],),
        local_diff=popped["diffs"][0],
        remote_diff=popped["diffs"][1],
        action=decision.action,
        conflict=decision.conflict)
    if decision.action == "custom":
        ret.custom_diff = popped["diffs"][2]
    return ret


def pop_all_patch_decisions(decision):
    """Create a new decision at the furthest level in the diff tree.

    Calls `pop_patch_decision` recursively until it is at the lowest possible
    decision level.

    If the decision is already at the lowest level, it returns the original
    decision.
    """
    popped = pop_patch_decision(decision)
    while popped is not None:
        decision = popped
        popped = pop_patch_decision(decision)
    return decision


def push_patch_decision(decision, prefix):
    """Move a path prefix in a merge decision from `common_path` to the diffs.

    This is done by wrapping the diffs in nested patch ops.
    """
    dec = copy.copy(decision)
    # We need to start with inner most key to nest correctly, so reverse:
    for key in reversed(prefix):
        if len(dec.common_path) == 0:
            raise ValueError(
                "Cannot remove key from empty decision path: %s, %s" %
                (key, dec))
        assert dec.common_path[-1] == key, "Key %s not at end of %s" % (
            key, dec.common_path)
        dec.common_path = dec.common_path[:-1]  # pop key
        dec.local_diff = [op_patch(key, dec.local_diff)]
        dec.remote_diff = [op_patch(key, dec.remote_diff)]
        if dec.action == "custom":
            dec.custom_diff = [op_patch(key, dec.custom_diff)]
    return dec


def _sort_key(k):
    """Sort key for common paths. Ensures the correct order for processing,
    without having to care about offsetting indices.

    Heavily inspired by the natsort package:

    Copyright (c) 2012-2016 Seth M. Morton

    Permission is hereby granted, free of charge, to any person obtaining a copy of
    this software and associated documentation files (the "Software"), to deal in
    the Software without restriction, including without limitation the rights to
    use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
    of the Software, and to permit persons to whom the Software is furnished to do
    so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
    """
    ret = []
    for s in k.common_path:
        if not isinstance(s, (int, text_type)):
            s = s.decode("utf8")
        if isinstance(s, text_type) and r_is_int.match(s):
            s = int(s)
        if isinstance(s, int):
            ret.append(('', -s))
        else:
            ret.append((s,))
    return ret


def split_string_path(base, path):
    """Prevent paths from pointing to specific string lines.

    Since strings are diffed as a sequence of lines without actually splitting
    the string in base, any path to a specific line will fail to resolve.
    This checks if path points to a specific line in a string, and splits off
    the final key of the path (the line number).

    Returns a tuple with the path without any line reference in the first
    position, and any line key in the second position.
    """
    for i in range(len(path)):
        if isinstance(base, string_types):
            return path[:i], path[i:]
        base = base[path[i]]
    return path, ()


def make_cleared_value(value):
    "Make a new 'cleared' value of the right type."
    if isinstance(value, list):
        # Clearing e.g. an outputs list means setting it to an empty list
        return []
    elif isinstance(value, dict):
        # Clearing e.g. a metadata dict means setting it to an empty dict
        return {}
    elif isinstance(value, string_types):
        # Clearing e.g. a source string means setting it to an empty string
        return ""
    else:
        # Clearing anything else (atomic values) means setting it to None
        return None



def filter_decisions(pattern, decisions, exact):
    ret = []
    cutoff = len(pattern)
    for i, md in enumerate(decisions):
        path = md.common_path[:]
        pop = _pop_path((md.local_diff, md.remote_diff, md.custom_diff))
        if pop:
            path.push(pop)
        starred_path = star_path(path)
        if exact and star_path == pattern or star_path[:cutoff] == pattern:
            ret.append(i)
    return ret


# =============================================================================
#
# Code for applying decisions:
#
# =============================================================================

def resolve_action(base, decision):
    a = decision.action
    if a == "base":
        return []   # no-op
    elif a in ("local", "either"):
        return copy.copy(decision.local_diff)
    elif a == "remote":
        return copy.copy(decision.remote_diff)
    elif a == "custom":
        return copy.copy(decision.custom_diff)
    elif a == "local_then_remote":
        return decision.local_diff + decision.remote_diff
    elif a == "remote_then_local":
        return decision.remote_diff + decision.local_diff
    elif a == "clear":
        key = None
        for d in decision.local_diff + decision.remote_diff:
            if key:
                assert key == d.key
            else:
                key = d.key
        return [op_replace(key, make_cleared_value(base[key]))]
    elif a == "clear_parent":
        if isinstance(base, dict):
            # Ideally we would do a op_replace on the parent, but this is not
            # easily combined with this method, so simply remove all keys
            return [op_remove(key) for key in base.keys()]
        elif isinstance(base, (list,) + string_types):
            return [op_removerange(0, len(base))]

    else:
        raise NotImplementedError("The action \"%s\" is not defined" % a)


def apply_decisions(base, decisions):
    """Apply a list of merge decisions to base.
    """
    merged = copy.deepcopy(base)
    prev_path = None
    parent = None
    last_key = None
    resolved = None
    diffs = None
    # clear_parent actions should override other decisions on same obj, so
    # we need to track it
    clear_parent_flag = False
    for md in decisions:
        path, line = split_string_path(merged, md.common_path)
        # We patch all decisions with the same path in one op
        if path == prev_path:
            # Same path as previous, collect entry
            if clear_parent_flag:
                # Another entry will clear the parent, all other decisions
                # should be dropped
                pass
            else:
                if md.action == "clear_parent":
                    clear_parent_flag = True
                    # Clear any exisiting decisions!
                    diffs = []
                ad = resolve_action(resolved, md)
                if line:
                    ad = push_path(line, ad)
                diffs.extend(ad)

        else:
            # Different path, start a new collection
            if prev_path is not None:
                # First, apply previous diffs
                if parent is None:
                    # Operations on root create new merged object
                    merged = patch(resolved, diffs)
                else:
                    # If not, overwrite entry in parent (which is an entry in
                    # merged). This is ok, as no paths should point to
                    # subobjects of the patched object
                    parent[last_key] = patch(resolved, diffs)

            prev_path = path
            # Resolve path in base and output
            resolved = merged
            parent = None
            last_key = None
            for key in path:
                parent = resolved
                resolved = resolved[key]   # Should raise if key missing
                last_key = key
            diffs = resolve_action(resolved, md)
            if line:
                diffs = push_path(line, diffs)
            clear_parent_flag = md.action == "clear_parent"
    # Apply the last collection of diffs, if present (same as above)
    if prev_path is not None:
        if parent is None:
            merged = patch(resolved, diffs)
        else:
            parent[last_key] = patch(resolved, diffs)

    merged = nbformat.from_dict(merged)
    return merged


def _merge_tree(tree, sorted_paths):
    """
    Merge a tree of diffs at varying path levels to one diff at their shared root

    Relies on the format specification about decision ordering to help
    simplify the process (deeper paths should come before its parent paths).
    This is realized by the `sorted_paths` argument.
    """

    trunk = []
    root = None
    for i in range(len(sorted_paths)):
        pathStr = sorted_paths[i]
        path = tree[pathStr].path
        nextPath = None
        if i == len(sorted_paths) - 1:
            nextPath = root
        else:
            nextPathStr = sorted_paths[i + 1]
            nextPath = tree[nextPathStr].path

        subdiffs = tree[pathStr].diff
        trunk = trunk + subdiffs
        # First, check if path is subpath of nextPath:
        if is_prefix_array(nextPath, path):
            # We can simply promote existing diffs to next path
            if nextPath is not None:
                trunk = push_path(path[nextPath.length:], trunk)
                root = nextPath
        else:
            # We have started on a new trunk
            # Collect branches on the new trunk, and merge the trunks
            newTrunk = _mergeTree(tree, sorted_paths[i + 1])
            nextPath = tree[sorted_paths[sorted_paths.length - 1]].path
            prefix = findSharedPrefix(path, nextPath)
            pl = len(prefix) if prefix is not None else 0
            trunk = push_path(path[pl:], trunk) + push_path(nextPath[pl:], newTrunk)
            break   # Recursion will exhaust sorted_paths
    return trunk


def build_diffs(base, decisions, which):
    """
    Builds a diff for direct application on base. The `which` argument either
    selects the 'local', 'remote' or 'merged' diffs.
    """
    tree = {}
    sorted_paths = []
    local = which == 'local'
    merged = which == 'merged'
    if not local and not merged:
        assert which == 'remote'

    for md in decisions:
        subdiffs = None
        path, line = split_string_path(base, md.local_path())
        if merged:
            sub = base[path]
            subdiffs = resolve_action(sub, md)
        else:
            subdiffs = md.local_diff if local else md.remote_diff
            if subdiffs is None:
                continue

        str_path = join_path(path)
        if str_path in tree:
            # Existing tree entry, simply add diffs to it
            if line:
                match_diff = [d for d in tree[str_path].diff if d.key == line[0]]
                if match_diff:
                    subdiffs.extend(match_diff)
                else:
                    subdiffs = push_path(line, subdiffs)
                    tree[str_path].diff.append(subdiffs[0])

            else:
                tree[str_path].diff.extend(subdiffs)

        else:
            # Make new entry in tree
            if line:
                subdiffs = push_path(line, subdiffs)
            tree[str_path] = {'diff': subdiffs, 'path': path}
            sorted_paths.push(str_path)

    if len(tree) == 0:
        return None

    if '/' not in tree:
        tree['/'] = {diff: [], path: []}
        sorted_paths.append('/')

    # Tree is constructed, now join all branches at diverging points (joints)
    return _merge_tree(tree, sorted_paths)
