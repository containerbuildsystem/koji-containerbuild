"""
Copyright (C) 2019  Red Hat, Inc.

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
"""

import koji
import pytest
from flexmock import flexmock

from koji_containerbuild.plugins import hub_containerbuild


@pytest.mark.parametrize('admin_perms', [True, False])
@pytest.mark.parametrize('priority', [1, 0, None, -1])
def test_priority_permissions(priority, admin_perms):
    src, target = 'source', 'target'

    task_opts = {}
    if priority:
        task_opts['priority'] = koji.PRIO_DEFAULT + priority

    session = flexmock()
    (session
        .should_receive('hasPerm')
        .with_args('admin')
        .and_return(admin_perms))
    hub_containerbuild.context = flexmock(session=session)

    should_succeed = priority is None or priority >= 0 or admin_perms

    kojihub = flexmock()
    (kojihub
        .should_receive('make_task')
        .with_args('buildContainer', [src, target, {}],
                   channel='container', **task_opts)
        .times(1 if should_succeed else 0))
    hub_containerbuild.kojihub = kojihub

    if should_succeed:
        hub_containerbuild.buildContainer(src, target, priority=priority)
    else:
        with pytest.raises(koji.ActionNotAllowed) as exc_info:
            hub_containerbuild.buildContainer(src, target, priority=priority)

        e = exc_info.value
        assert str(e) == 'only admins may create high-priority tasks'
