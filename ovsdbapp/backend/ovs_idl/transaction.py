# Copyright (c) 2017 Red Hat Inc
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

import logging
import time

from ovs.db import idl
from six.moves import queue as Queue

from ovsdbapp import api
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp import exceptions

LOG = logging.getLogger(__name__)


class Transaction(api.Transaction):
    def __init__(self, api, ovsdb_connection, timeout=None,
                 check_error=False, log_errors=True):
        self.api = api
        self.check_error = check_error
        self.log_errors = log_errors
        self.commands = []
        self.results = Queue.Queue(1)
        self.ovsdb_connection = ovsdb_connection
        self.timeout = timeout or ovsdb_connection.timeout

    def __str__(self):
        return ", ".join(str(cmd) for cmd in self.commands)

    def add(self, command):
        """Add a command to the transaction

        returns The command passed as a convenience
        """
        #仅实现将command加入到commands缓存中
        self.commands.append(command)
        return command

    def commit(self):
        self.ovsdb_connection.queue_txn(self)
        try:
            result = self.results.get(timeout=self.timeout)
        except Queue.Empty:
            raise exceptions.TimeoutException(commands=self.commands,
                                              timeout=self.timeout)
        if isinstance(result, idlutils.ExceptionResult):
            if self.log_errors:
                LOG.error(result.tb)
            if self.check_error:
                raise result.ex
        return result

    def pre_commit(self, txn):
        pass

    def post_commit(self, txn):
        for command in self.commands:
            command.post_commit(txn)

    def do_commit(self):
        self.start_time = time.time()
        attempts = 0
        while True:
            if attempts > 0 and self.timeout_exceeded():
                raise RuntimeError("OVS transaction timed out")
            attempts += 1
            # TODO(twilson) Make sure we don't loop longer than vsctl_timeout
            txn = idl.Transaction(self.api.idl)
            #调用pre_commit函数
            self.pre_commit(txn)
            #遍历所有缓存的命令，调用命令对应的run_idl函数
            for i, command in enumerate(self.commands):
                LOG.debug("Running txn n=%(n)d command(idx=%(idx)s): %(cmd)s",
                          {'idx': i, 'cmd': command, 'n': attempts})
                try:
                    command.run_idl(txn)
                except Exception:
                    #出错，事务中止
                    txn.abort()
                    if self.check_error:
                        raise
            #事务提交
            status = txn.commit_block()
            if status == txn.TRY_AGAIN:
                #要求稍后重试
                LOG.debug("OVSDB transaction returned TRY_AGAIN, retrying")
                # In the case that there is a reconnection after
                # Connection.run() calls self.idl.run() but before do_commit()
                # is called, commit_block() can loop w/o calling idl.run()
                # which does the reconnect logic. It will then always return
                # TRY_AGAIN until we time out and Connection.run() calls
                # idl.run() again. So, call idl.run() here just in case.
                self.api.idl.run()
                continue
            elif status in (txn.ERROR, txn.NOT_LOCKED):
                msg = 'OVSDB Error: '
                #事务提交出现error
                if status == txn.NOT_LOCKED:
                    msg += ("The transaction failed because the IDL has "
                            "been configured to require a database lock "
                            "but didn't get it yet or has already lost it")
                else:
                    msg += txn.get_error()

                if self.log_errors:
                    LOG.error(msg)
                if self.check_error:
                    # For now, raise similar error to vsctl/utils.execute()
                    raise RuntimeError(msg)
                return
            elif status == txn.ABORTED:
                LOG.debug("Transaction aborted")
                return #为什么这种不需要扔异常出来？
            elif status == txn.UNCHANGED:
                #数据库无变更
                LOG.debug("Transaction caused no change")
            elif status == txn.SUCCESS:
                self.post_commit(txn)
            else:
                LOG.debug("Transaction returned an unknown status: %s", status)

            #返回成功执行的commands
            return [cmd.result for cmd in self.commands]

    def elapsed_time(self):
        return time.time() - self.start_time

    def time_remaining(self):
        return self.timeout - self.elapsed_time()

    def timeout_exceeded(self):
        return self.elapsed_time() > self.timeout
