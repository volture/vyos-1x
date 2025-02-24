#!/usr/bin/env python3
#
# Copyright (C) 2022-2023 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

import os
import sys
from tabulate import tabulate

import vyos.opmode
from vyos.util import bytes_to_human
from vyos.util import commit_in_progress
from vyos.util import call
from vyos.config import Config

def _get_tunnel_address(peer_host, peer_port, status_file):
    peer = peer_host + ':' + peer_port
    lst = []

    with open(status_file, 'r') as f:
        lines = f.readlines()
        for line in lines:
            if peer in line:
                lst.append(line)

        # filter out subnet entries if iroute:
        # in the case that one sets, say:
        # [ ..., 'vtun10', 'server', 'client', 'client1', 'subnet','10.10.2.0/25']
        # the status file will have an entry:
        # 10.10.2.0/25,client1,...
        lst = [l for l in lst[1:] if '/' not in l.split(',')[0]]

        tunnel_ip = lst[0].split(',')[0]

        return tunnel_ip

def _get_interface_status(mode: str, interface: str) -> dict:
    status_file = f'/run/openvpn/{interface}.status'

    data = {
        'mode': mode,
        'intf': interface,
        'local_host': '',
        'local_port': '',
        'date': '',
        'clients': [],
    }

    if not os.path.exists(status_file):
        raise vyos.opmode.DataUnavailable('No information for interface {interface}')

    with open(status_file, 'r') as f:
        lines = f.readlines()
        for line_no, line in enumerate(lines):
            # remove trailing newline character first
            line = line.rstrip('\n')

            # check first line header
            if line_no == 0:
                if mode == 'server':
                    if not line == 'OpenVPN CLIENT LIST':
                        raise vyos.opmode.InternalError('Expected "OpenVPN CLIENT LIST"')
                else:
                    if not line == 'OpenVPN STATISTICS':
                        raise vyos.opmode.InternalError('Expected "OpenVPN STATISTICS"')

                continue

            # second line informs us when the status file has been last updated
            if line_no == 1:
                data['date'] = line.lstrip('Updated,').rstrip('\n')
                continue

            if mode == 'server':
                # for line_no > 1, lines appear as follows:
                #
                # Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since
                # client1,172.18.202.10:55904,2880587,2882653,Fri Aug 23 16:25:48 2019
                # client3,172.18.204.10:41328,2850832,2869729,Fri Aug 23 16:25:43 2019
                # client2,172.18.203.10:48987,2856153,2871022,Fri Aug 23 16:25:45 2019
                # ...
                # ROUTING TABLE
                # ...
                if line_no >= 3:
                    # indicator that there are no more clients
                    if line == 'ROUTING TABLE':
                        break
                    # otherwise, get client data
                    remote = (line.split(',')[1]).rsplit(':', maxsplit=1)

                    client = {
                        'name': line.split(',')[0],
                        'remote_host': remote[0],
                        'remote_port': remote[1],
                        'tunnel': 'N/A',
                        'rx_bytes': bytes_to_human(int(line.split(',')[2]),
                                                   precision=1),
                        'tx_bytes': bytes_to_human(int(line.split(',')[3]),
                                                   precision=1),
                        'online_since': line.split(',')[4]
                    }
                    client['tunnel'] = _get_tunnel_address(client['remote_host'],
                                                           client['remote_port'],
                                                           status_file)
                    data['clients'].append(client)
                    continue
            else: # mode == 'client' or mode == 'site-to-site'
                if line_no == 2:
                    client = {
                        'name': 'N/A',
                        'remote_host': 'N/A',
                        'remote_port': 'N/A',
                        'tunnel': 'N/A',
                        'rx_bytes': bytes_to_human(int(line.split(',')[1]),
                                                   precision=1),
                        'tx_bytes': '',
                        'online_since': 'N/A'
                    }
                    continue

                if line_no == 3:
                    client['tx_bytes'] = bytes_to_human(int(line.split(',')[1]),
                                                        precision=1)
                    data['clients'].append(client)
                    break

    return data

def _get_raw_data(mode: str) -> dict:
    data = {}
    conf = Config()
    conf_dict = conf.get_config_dict(['interfaces', 'openvpn'],
                                     get_first_key=True)
    if not conf_dict:
        return data

    interfaces = [x for x in list(conf_dict) if conf_dict[x]['mode'] == mode]
    for intf in interfaces:
        data[intf] = _get_interface_status(mode, intf)
        d = data[intf]
        d['local_host'] = conf_dict[intf].get('local-host', '')
        d['local_port'] = conf_dict[intf].get('local-port', '')
        if conf.exists(f'interfaces openvpn {intf} server client'):
            d['configured_clients'] = conf.list_nodes(f'interfaces openvpn {intf} server client')
        if mode in ['client', 'site-to-site']:
            for client in d['clients']:
                if 'shared-secret-key-file' in list(conf_dict[intf]):
                    client['name'] = 'None (PSK)'
                client['remote_host'] = conf_dict[intf].get('remote-host', [''])[0]
                client['remote_port'] = conf_dict[intf].get('remote-port', '1194')

    return data

def _format_openvpn(data: dict) -> str:
    if not data:
        out = 'No OpenVPN interfaces configured'
        return out

    headers = ['Client CN', 'Remote Host', 'Tunnel IP', 'Local Host',
               'TX bytes', 'RX bytes', 'Connected Since']

    out = ''
    for intf in list(data):
        data_out = []
        l_host = data[intf]['local_host']
        l_port = data[intf]['local_port']
        for client in list(data[intf]['clients']):
            r_host = client['remote_host']
            r_port = client['remote_port']

            out += f'\nOpenVPN status on {intf}\n\n'
            name = client['name']
            remote = r_host + ':' + r_port if r_host and r_port else 'N/A'
            tunnel = client['tunnel']
            local = l_host + ':' + l_port if l_host and l_port else 'N/A'
            tx_bytes = client['tx_bytes']
            rx_bytes = client['rx_bytes']
            online_since = client['online_since']
            data_out.append([name, remote, tunnel, local, tx_bytes,
                             rx_bytes, online_since])

        if data_out:
            out += tabulate(data_out, headers)
            out += "\n"

    return out

def show(raw: bool, mode: str) -> str:
    openvpn_data = _get_raw_data(mode)

    if raw:
        return openvpn_data

    return _format_openvpn(openvpn_data)

def reset(interface: str):
    if os.path.isfile(f'/run/openvpn/{interface}.conf'):
        if commit_in_progress():
            raise vyos.opmode.CommitInProgress('Retry OpenVPN reset: commit in progress.')
        call(f'systemctl restart openvpn@{interface}.service')
    else:
        raise vyos.opmode.IncorrectValue(f'OpenVPN interface "{interface}" does not exist!')

if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
