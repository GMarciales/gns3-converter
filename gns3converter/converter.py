# Copyright (C) 2014 Daniel Lintott.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
"""
 This class is the main gns3-converter class
"""
from configobj import ConfigObj, flatten_errors
from validate import Validator
import sys
from ipaddress import ip_address
from pkg_resources import resource_stream
from gns3converter.adapters import PORT_TYPES
from gns3converter.models import MODEL_TRANSFORM
from gns3converter.node import Node
from gns3converter.interfaces import INTERFACE_RE
from gns3converter.topology import LegacyTopology


class Converter():
    """
    GNS3 Topology Converter Class
    :param topology: Filename of the ini-style topology
    """
    def __init__(self, topology, debug=False):
        self._topology = topology
        self._debug = debug

        self.port_id = 1
        self.links = []
        self.configs = []

    def read_topology(self):
        """
        Read the ini-style topology file using ConfigObj
        :return: config
        """
        configspec = resource_stream(__name__, 'configspec')
        config = None
        try:
            handle = open(self._topology)
            handle.close()
            try:
                config = ConfigObj(self._topology,
                                   configspec=configspec,
                                   raise_errors=True,
                                   list_values=False,
                                   encoding='utf-8')
            except SyntaxError:
                print('Error loading .net file')
                exit()
        except IOError:
            print('Can\'t open topology file')
            exit()

        vtor = Validator()
        res = config.validate(vtor, preserve_errors=True)
        if res and self._debug:
            print('Validation Passed')
        else:
            for entry in flatten_errors(config, res):
                # each entry is a tuple
                (section_list, key, error) = entry
                if key is not None:
                    section_list.append(key)
                else:
                    section_list.append('[missing section]')
                section_string = ', '.join(section_list)

                if error is False:
                    error = 'Missing value or section'
                print(section_string, ' = ', error)
                input('Press ENTER to continue')
                sys.exit(1)

        configspec.close()
        return config

    @staticmethod
    def process_topology(sections, old_top):
        """
        Processes the sections returned by get_instances
        :param sections: A list of sections as generated by get_instances()
        :param old_top: The old topology as processed by read_topology()
        :return: devices, conf
        """
        topo = LegacyTopology(sections, old_top)

        for instance in sorted(sections):
            for item in sorted(old_top[instance]):
                if isinstance(old_top[instance][item], dict):
                    if item in MODEL_TRANSFORM:
                        # A configuration item
                        topo.add_conf_item(instance, item)
                    else:
                        # It must be a physical item
                        topo.add_physical_item(instance, item)
            topo.hv_id += 1
        return topo.devices, topo.conf

    @staticmethod
    def get_instances(config):
        """
        Get a list of Hypervisor instances
        :param config: Configuration from read_topology()
        :return: instances
        """
        instances = []
        for item in sorted(config):
            if ':' in item:
                delim_pos = item.index(':')
                addr = item[0:delim_pos]
                try:
                    ip_address(addr)
                    instances.append(item)
                except ValueError:
                    pass
        return instances

    def generate_nodes(self, devices, hypervisors):
        """
        Generate a list of nodes for the new topology
        :param devices:
        :param hypervisors:
        :return: nodes
        """
        nodes = []

        for device in sorted(devices):
            hv_id = devices[device]['hv_id']
            tmp_node = Node(hypervisors[hv_id], self.port_id)
            # Start building the structure
            tmp_node.node['properties']['name'] = device
            tmp_node.node['label']['text'] = device
            tmp_node.node['id'] = devices[device]['node_id']
            tmp_node.node['x'] = devices[device]['x']
            tmp_node.node['y'] = devices[device]['y']
            tmp_node.device_info['type'] = devices[device]['type']

            if 'model' in devices[device]:
                tmp_node.device_info['model'] = devices[device]['model']
            else:
                tmp_node.device_info['model'] = ''

            tmp_node.set_description()
            tmp_node.set_type()

            # Now lets process the rest
            for item in sorted(devices[device]):
                tmp_node.add_device_items(item, devices[device])

            if tmp_node.device_info['type'] == 'Router':
                tmp_node.add_info_from_hv()
                tmp_node.node['router_id'] = devices[device]['node_id']
                tmp_node.calc_mb_ports()

                for item in sorted(tmp_node.node['properties']):
                    if item.startswith('slot'):
                        tmp_node.add_slot_ports(item)
                    elif item.startswith('wic'):
                        tmp_node.add_wic_ports(item)

                # Add default ports to 7200 and 3660
                if tmp_node.device_info['model'] == 'c7200':
                    tmp_node.add_slot_ports('slot0')
                elif tmp_node.device_info['model'] == 'c3600' \
                        and tmp_node.device_info['chassis'] == '3660':
                    tmp_node.node['properties']['slot0'] = 'Leopard-2FE'

                # Calculate the router links
                tmp_node.calc_router_links()

            elif tmp_node.device_info['type'] == 'Cloud':
                tmp_node.calc_cloud_connection()

            # Get the data we need back from the node instance
            self.links.extend(tmp_node.links)
            self.configs.extend(tmp_node.config)
            self.port_id += tmp_node.get_nb_added_ports(self.port_id)

            nodes.append(tmp_node.node)

        return nodes

    def generate_links(self, nodes):
        """
        Generate a list of links
        :param nodes
        :return: links
        """
        links = []

        for link in self.links:
            # Expand port name if required
            if INTERFACE_RE.search(link['dest_port']):
                int_type = link['dest_port'][0]
                dest_port = link['dest_port'].replace(
                    int_type, PORT_TYPES[int_type.upper()])
            else:
                dest_port = link['dest_port']

            #Convert dest_dev to destination_node_id
            (dest_node, dest_port_id) = self.convert_destination_to_id(
                link['dest_dev'], dest_port, nodes)

            desc = 'Link from %s port %s to %s port %s' % \
                   (link['source_dev'], link['source_port_name'],
                    dest_node['name'], dest_port)

            links.append({'description': desc,
                          'destination_node_id': dest_node['id'],
                          'destination_port_id': dest_port_id,
                          'source_port_id': link['source_port_id'],
                          'source_node_id': link['source_node_id']})

        # Remove duplicate links and add link_id
        link_id = 1
        for link in links:
            t_link = str(link['source_node_id']) + ':' + \
                str(link['source_port_id'])
            for link2 in links:
                d_link = str(link2['destination_node_id']) + ':' + \
                    str(link2['destination_port_id'])
                if t_link == d_link:
                    links.remove(link2)
                    break
            link['id'] = link_id
            link_id += 1

            self.add_node_connections(link, nodes)
        return links

    @staticmethod
    def device_id_from_name(device_name, nodes):
        """
        Get the device ID when given a device name
        :param device_name:
        :param nodes:
        :return: device_id
        """
        device_id = None
        for node in nodes:
            if device_name == node['properties']['name']:
                device_id = node['id']
                break
        return device_id

    @staticmethod
    def port_id_from_name(port_name, device_id, nodes):
        """
        Get the port ID when given a port name
        :param port_name:
        :param device_id:
        :param nodes:
        :return: port_id
        """
        port_id = None
        for node in nodes:
            if device_id == node['id']:
                for port in node['ports']:
                    if port_name == port['name']:
                        port_id = port['id']
                        break
                break
        return port_id

    @staticmethod
    def convert_destination_to_id(destination_node, destination_port, nodes):
        """
        Convert a destination to device and port ID
        :param destination_node:
        :param destination_port:
        :param nodes:
        :return: device_id, port_id
        """
        device_id = None
        device_name = None
        port_id = None
        if destination_node != 'NIO':
            for node in nodes:
                if destination_node == node['properties']['name']:
                    device_id = node['id']
                    device_name = destination_node
                    for port in node['ports']:
                        if destination_port == port['name']:
                            port_id = port['id']
                            break
                    break
        else:
            for node in nodes:
                if node['type'] == 'Cloud':
                    for port in node['ports']:
                        if destination_port == port['name']:
                            device_id = node['id']
                            device_name = node['properties']['name']
                            port_id = port['id']
                            break
                    break
        device = {'id': device_id,
                  'name': device_name}
        return device, port_id

    @staticmethod
    def get_node_name_from_id(node_id, nodes):
        """
        Get the name of a node when given the node_id
        :param node_id: The ID of a node
        :param nodes: A list of nodes dicts
        :return: node_name
        """
        node_name = ''
        for node in nodes:
            if node['id'] == node_id:
                node_name = node['properties']['name']
                break
        return node_name

    @staticmethod
    def get_port_name_from_id(node_id, port_id, nodes):
        """
        Get the name of a port for a given node and port ID
        :param node_id: The UID of a node
        :param port_id: The UID of a port
        :param nodes: A list of nodes dicts
        :return: port_name
        """
        port_name = ''
        for node in nodes:
            if node['id'] == node_id:
                for port in node['ports']:
                    if port['id'] == port_id:
                        port_name = port['name']
                        break
        return port_name

    def add_node_connections(self, link, nodes):
        """
        Add connections to the node items
        :param link:
        :param nodes:
        """
        # Description
        src_desc = 'connected to %s on port %s' % \
                   (self.get_node_name_from_id(link['destination_node_id'],
                                               nodes),
                    self.get_port_name_from_id(link['destination_node_id'],
                                               link['destination_port_id'],
                                               nodes))
        dest_desc = 'connected to %s on port %s' % \
                    (self.get_node_name_from_id(link['source_node_id'],
                                                nodes),
                     self.get_port_name_from_id(link['source_node_id'],
                                                link['source_port_id'],
                                                nodes))
        # Add source connections
        for node in nodes:
            if node['id'] == link['source_node_id']:
                for port in node['ports']:
                    if port['id'] == link['source_port_id']:
                        port['link_id'] = link['id']
                        port['description'] = src_desc
                        break
            elif node['id'] == link['destination_node_id']:
                for port in node['ports']:
                    if port['id'] == link['destination_port_id']:
                        port['link_id'] = link['id']
                        port['description'] = dest_desc
                        break
