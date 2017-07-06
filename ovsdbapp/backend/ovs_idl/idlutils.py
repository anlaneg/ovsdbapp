# Copyright (c) 2015 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import os
import sys
import time
import uuid

from ovs.db import idl
from ovs import jsonrpc
from ovs import poller
from ovs import stream
import six

from ovsdbapp import api
from ovsdbapp import exceptions


RowLookup = collections.namedtuple('RowLookup',
                                   ['table', 'column', 'uuid_column'])

# Tables with no index in OVSDB and special record lookup rules
_LOOKUP_TABLE = {
    'Controller': RowLookup('Bridge', 'name', 'controller'),
    'Flow_Table': RowLookup('Flow_Table', 'name', None),
    'IPFIX': RowLookup('Bridge', 'name', 'ipfix'),
    'Mirror': RowLookup('Mirror', 'name', None),
    'NetFlow': RowLookup('Bridge', 'name', 'netflow'),
    'Open_vSwitch': RowLookup('Open_vSwitch', None, None),
    'QoS': RowLookup('Port', 'name', 'qos'),
    'Queue': RowLookup(None, None, None),
    'sFlow': RowLookup('Bridge', 'name', 'sflow'),
    'SSL': RowLookup('Open_vSwitch', None, 'ssl'),
}

_NO_DEFAULT = object()


class RowNotFound(exceptions.OvsdbAppException):
    message = "Cannot find %(table)s with %(col)s=%(match)s"


def row_by_value(idl_, table, column, match, default=_NO_DEFAULT):
    """Lookup an IDL row in a table by column/value"""
    tab = idl_.tables[table]
    for r in tab.rows.values():
        if getattr(r, column) == match:
            return r
    if default is not _NO_DEFAULT:
        return default
    raise RowNotFound(table=table, col=column, match=match)


def row_by_record(idl_, table, record):
    t = idl_.tables[table]
    try:
        if isinstance(record, uuid.UUID):
            return t.rows[record]
        uuid_ = uuid.UUID(record)
        return t.rows[uuid_]
    except ValueError:
        # Not a UUID string, continue lookup by other means
        pass
    except KeyError:
        if sys.platform != 'win32':
            # On Windows the name of the ports is described by the OVS schema:
            # https://tinyurl.com/zk8skhx
            # Is a UUID. (This is due to the fact on Windows port names don't
            # have the 16 chars length limitation as for Linux). Because of
            # this uuid.UUID(record) will not raise a ValueError exception
            # as it happens on Linux and will try to fetch the directly
            # the column instead of using the lookup table. This will raise
            # a KeyError exception on Windows.
            raise RowNotFound(table=table, col='uuid', match=record)

    rl = _LOOKUP_TABLE.get(table, RowLookup(table, get_index_column(t), None))
    # no table means uuid only, no column means lookup table only has one row
    if rl.table is None:
        raise ValueError("Table %s can only be queried by UUID") % table
    if rl.column is None:
        return next(iter(t.rows.values()))
    row = row_by_value(idl_, rl.table, rl.column, record)
    if rl.uuid_column:
        rows = getattr(row, rl.uuid_column)
        if len(rows) != 1:
            raise RowNotFound(table=table, col='record', match=record)
        row = rows[0]
    return row


class ExceptionResult(object):
    def __init__(self, ex, tb):
        self.ex = ex
        self.tb = tb


def get_schema_helper(connection, schema_name):
    """Create a schema helper object by querying an ovsdb-server

    :param connection: The ovsdb-server connection string
    :type connection: string
    :param schema_name: The schema on the server to pull
    :type schema_name: string
    """
    err, strm = stream.Stream.open_block(
        stream.Stream.open(connection))
    if err:
        #有错误，扔异常
        raise Exception("Could not connect to %s" % connection)
    rpc = jsonrpc.Connection(strm)
    req = jsonrpc.Message.create_request('get_schema', [schema_name])
    err, resp = rpc.transact_block(req)
    rpc.close()
    if err:
        raise Exception("Could not retrieve schema from %(conn)s: "
                        "%(err)s" % {'conn': connection,
                                     'err': os.strerror(err)})
    elif resp.error:
        raise Exception(resp.error)
    return idl.SchemaHelper(None, resp.result)


def wait_for_change(_idl, timeout, seqno=None):
    if seqno is None:
        seqno = _idl.change_seqno
    stop = time.time() + timeout
    while _idl.change_seqno == seqno and not _idl.run():
        ovs_poller = poller.Poller()
        _idl.wait(ovs_poller)
        ovs_poller.timer_wait(timeout * 1000)
        ovs_poller.block()
        if time.time() > stop:
            raise Exception("Timeout")  # TODO(twilson) use TimeoutException?


def get_column_value(row, col):
    """Retrieve column value from the given row.

    If column's type is optional, the value will be returned as a single
    element instead of a list of length 1.
    """
    if col == '_uuid':
        val = row.uuid
    else:
        val = getattr(row, col)

    # Idl returns lists of Rows where ovs-vsctl returns lists of UUIDs
    if isinstance(val, list) and val:
        if isinstance(val[0], idl.Row):
            val = [v.uuid for v in val]
        col_type = row._table.columns[col].type
        # ovs-vsctl treats lists of 1 as single results
        if col_type.is_optional():
            val = val[0]
    return val


def condition_match(row, condition):
    """Return whether a condition matches a row

    :param row:       An OVSDB Row
    :param condition: A 3-tuple containing (column, operation, match)
    """
    col, op, match = condition
    val = get_column_value(row, col)

    # both match and val are primitive types, so type can be used for type
    # equality here.
    # NOTE (twilson) the above is a lie--not all string types are the same
    #                I haven't investigated the reason for the patch that
    #                added this code, but for now I check string_types
    if type(match) is not type(val) and not all(
        isinstance(x, six.string_types) for x in (match, val)):
        # Types of 'val' and 'match' arguments MUST match in all cases with 2
        # exceptions:
        # - 'match' is an empty list and column's type is optional;
        # - 'value' is an empty and  column's type is optional
        if (not all([match, val]) and
                row._table.columns[col].type.is_optional()):
            # utilize the single elements comparison logic
            if match == []:
                match = None
            elif val == []:
                val = None
        else:
            # no need to process any further
            raise ValueError(
                "Column type and condition operand do not match")

    matched = True

    # TODO(twilson) Implement other operators and type comparisons
    # ovs_lib only uses dict '=' and '!=' searches for now
    if isinstance(match, dict):
        for key in match:
            if op == '=':
                if key not in val or match[key] != val[key]:
                    matched = False
                    break
            elif op == '!=':
                if key not in val or match[key] == val[key]:
                    matched = False
                    break
            else:
                raise NotImplementedError()
    elif isinstance(match, list):
        # According to rfc7047, lists support '=' and '!='
        # (both strict and relaxed). Will follow twilson's dict comparison
        # and implement relaxed version (excludes/includes as per standard)
        if op == "=":
            if not all([val, match]):
                return val == match
            for elem in set(match):
                if elem not in val:
                    matched = False
                    break
        elif op == '!=':
            if not all([val, match]):
                return val != match
            for elem in set(match):
                if elem in val:
                    matched = False
                    break
        else:
            raise NotImplementedError()
    else:
        if op == '=':
            if val != match:
                matched = False
        elif op == '!=':
            if val == match:
                matched = False
        else:
            raise NotImplementedError()
    return matched


def row_match(row, conditions):
    """Return whether the row matches the list of conditions"""
    return all(condition_match(row, cond) for cond in conditions)


def get_index_column(table):
    if len(table.indexes) == 1:
        idx = table.indexes[0]
        if len(idx) == 1:
            return idx[0].name


def db_replace_record(obj):
    """Replace any api.Command objects with their results

    This method should leave obj untouched unless the object contains an
    api.Command object.
    """
    if isinstance(obj, collections.Mapping):
        for k, v in six.iteritems(obj):
            if isinstance(v, api.Command):
                obj[k] = v.result
    elif (isinstance(obj, collections.Sequence)
          and not isinstance(obj, six.string_types)):
        for i, v in enumerate(obj):
            if isinstance(v, api.Command):
                try:
                    obj[i] = v.result
                except TypeError:
                    # NOTE(twilson) If someone passes a tuple, then just return
                    # a tuple with the Commands replaced with their results
                    return type(obj)(getattr(v, "result", v) for v in obj)
    elif isinstance(obj, api.Command):
        obj = obj.result
    return obj
