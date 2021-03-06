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

from ovsdbapp.schema.open_vswitch import impl_idl
from ovsdbapp.tests.functional.schema import fixtures


class BridgeFixture(fixtures.ImplIdlFixture):
    api = impl_idl.OvsdbIdl
    create = 'add_br'
    delete = 'del_br'
    delete_id = 'name'
