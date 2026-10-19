#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""Which Zabbix release has which API method.

The tool catalog is one list for every configured server, but the
servers do not all speak the same API: ``proxygroup.*`` arrived in 7.0,
``templategroup.*`` in 6.2, ``host.massupdate`` leaves in 8.0. Without
this table a call to a method the server does not have comes back as a
bare ``Method not found``, and the model has no way to pick the right
tool for the right server up front.

``METHOD_SINCE`` lists the first release that has a method, for every
method that is *not* in 5.0 (the oldest release this server supports);
``METHOD_REMOVED`` lists the first release *without* it and what the
docs say to use instead. Both come from the per-release "API changes"
pages and the object reference pages, checked release by release
(5.0 ... 7.4, 8.0). A method missing from both tables exists on every
supported release.
"""

from __future__ import annotations

# First release that has the method (methods present in 5.0 are not listed).
METHOD_SINCE: dict[str, str] = {
    # Zabbix 5.2
    "authentication.get": "5.2",
    "authentication.update": "5.2",
    "housekeeping.get": "5.2",
    "housekeeping.update": "5.2",
    "role.create": "5.2",
    "role.delete": "5.2",
    "role.get": "5.2",
    "role.update": "5.2",
    "settings.get": "5.2",
    "settings.update": "5.2",
    "templatedashboard.create": "5.2",
    "templatedashboard.delete": "5.2",
    "templatedashboard.get": "5.2",
    "templatedashboard.update": "5.2",
    # Zabbix 5.4
    "configuration.importcompare": "5.4",
    "report.create": "5.4",
    "report.delete": "5.4",
    "report.get": "5.4",
    "report.update": "5.4",
    "token.create": "5.4",
    "token.delete": "5.4",
    "token.generate": "5.4",
    "token.get": "5.4",
    "token.update": "5.4",
    # Zabbix 6.0
    "hanode.get": "6.0",
    "history.clear": "6.0",
    "regexp.create": "6.0",
    "regexp.delete": "6.0",
    "regexp.get": "6.0",
    "regexp.update": "6.0",
    "sla.create": "6.0",
    "sla.delete": "6.0",
    "sla.get": "6.0",
    "sla.getsli": "6.0",
    "sla.update": "6.0",
    "user.unblock": "6.0",
    # Zabbix 6.2
    "hostgroup.propagate": "6.2",
    "templategroup.create": "6.2",
    "templategroup.delete": "6.2",
    "templategroup.get": "6.2",
    "templategroup.massadd": "6.2",
    "templategroup.massremove": "6.2",
    "templategroup.massupdate": "6.2",
    "templategroup.propagate": "6.2",
    "templategroup.update": "6.2",
    "userdirectory.create": "6.2",
    "userdirectory.delete": "6.2",
    "userdirectory.get": "6.2",
    "userdirectory.test": "6.2",
    "userdirectory.update": "6.2",
    # Zabbix 6.4
    "connector.create": "6.4",
    "connector.delete": "6.4",
    "connector.get": "6.4",
    "connector.update": "6.4",
    "module.create": "6.4",
    "module.delete": "6.4",
    "module.get": "6.4",
    "module.update": "6.4",
    "script.getscriptsbyevents": "6.4",
    "user.provision": "6.4",
    # Zabbix 7.0
    "history.push": "7.0",
    "mfa.create": "7.0",
    "mfa.delete": "7.0",
    "mfa.get": "7.0",
    "mfa.update": "7.0",
    "proxygroup.create": "7.0",
    "proxygroup.delete": "7.0",
    "proxygroup.get": "7.0",
    "proxygroup.update": "7.0",
    "user.resettotp": "7.0",
    # Zabbix 7.4
    "discoveryruleprototype.create": "7.4",
    "discoveryruleprototype.delete": "7.4",
    "discoveryruleprototype.get": "7.4",
    "discoveryruleprototype.update": "7.4",
}

# First release without the method -> (release, what to use instead).
METHOD_REMOVED: dict[str, tuple[str, str]] = {
    "host.massupdate": ("8.0", "host.update (per host)"),
    "hostgroup.massupdate": ("8.0", "hostgroup.update, hostgroup.massadd or hostgroup.massremove"),
    "hostinterface.replacehostinterfaces": ("8.0", "hostinterface.update, hostinterface.massadd or hostinterface.massremove"),
    "template.massupdate": ("8.0", "template.update, template.massadd or template.massremove"),
    "templategroup.massupdate": ("8.0", "templategroup.update, templategroup.massadd or templategroup.massremove"),
}


def parse_version(version: str | None) -> tuple[int, int] | None:
    """``"7.4.14"`` -> ``(7, 4)``; ``None`` for anything that is not a version."""
    if not version:
        return None
    try:
        parts = str(version).strip().split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None


def unavailable_reason(api_method: str, version: str | None) -> str | None:
    """Why *api_method* cannot be called on a server running *version*, or ``None``.

    An unknown or unparseable version never blocks a call: the server
    then answers with its own error, as before.
    """
    current = parse_version(version)
    if current is None:
        return None
    method = api_method.lower()
    since = METHOD_SINCE.get(method)
    if since and current < parse_version(since):
        return f"{api_method} requires Zabbix {since} or later (this server runs {version})."
    removed = METHOD_REMOVED.get(method)
    if removed and current >= parse_version(removed[0]):
        return (
            f"{api_method} was removed in Zabbix {removed[0]} (this server runs {version}). "
            f"Use {removed[1]} instead."
        )
    return None
