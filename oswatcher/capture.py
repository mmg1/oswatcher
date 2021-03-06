import json
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory, gettempdir
from timeit import default_timer as timer

import libvirt
from py2neo import Graph
from see import Environment
from see.context import QEMUContextFactory

from oswatcher.model import OS
from oswatcher.utils import get_hard_drive_path

__SCRIPT_DIR = os.path.dirname(os.path.realpath(sys.argv[0]))
DB_PASSWORD = "admin"
DESKTOP_READY_DELAY = 180
SUBGRAPH_DELETE_OS = """
MATCH (o:OS)-[*0..]-(x)
WHERE o.name = "{}"
WITH DISTINCT x
DETACH DELETE x
"""


class QEMUDomainContextFactory(QEMUContextFactory):

    def __init__(self, domain_name, uri):
        # generate context.json and domain.xml
        self.domain_tmp_f = NamedTemporaryFile(mode='w')
        con = libvirt.open(uri)
        domain = con.lookupByName(domain_name)
        xml = domain.XMLDesc()
        self.domain_tmp_f.write(xml)
        self.domain_tmp_f.flush()
        # find domain qcow path
        qcow_path = Path(get_hard_drive_path(domain))
        # storage path
        self.osw_storage_path = TemporaryDirectory(prefix="osw-instances-",
                                                   dir=gettempdir())

        context_config = {
            "hypervisor": uri,
            "domain": {
                "configuration": self.domain_tmp_f.name
            },
            "disk": {
                "image": {
                    "provider": "see.image_providers.DummyProvider",
                    "provider_configuration": {
                        "path": qcow_path.parent,
                    },
                    "name": qcow_path.name,
                },
                "clone": {
                    "storage_pool_path": self.osw_storage_path.name,
                    "copy_on_write": True
                }
            }
        }
        super().__init__(context_config)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.osw_storage_path.cleanup()
        self.domain_tmp_f.close()


def protocol(environment):
    context = environment.context
    config = environment.configuration['configuration']
    context.trigger('protocol_start')
    context.trigger('offline')
    desktop_ready_delay = config['desktop_ready_delay']
    if desktop_ready_delay > 0:
        # start domain
        logging.info("Starting the domain")
        context.poweron()
        # wait until desktop is ready
        logging.debug("Waiting %d seconds for desktop to be ready",
                      config['desktop_ready_delay'])
        time.sleep(config['desktop_ready_delay'])
        context.trigger('desktop_ready')
        # shutdown
        logging.info("Shutting down the domain")
        context.poweroff()
    context.trigger('protocol_end')


def init_logger(debug=False):
    formatter = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
    logging_level = logging.INFO
    if debug:
        logging_level = logging.DEBUG
    logging.basicConfig(level=logging_level, format=formatter)
    # suppress annoying log output
    logging.getLogger("httpstream").setLevel(logging.WARNING)
    logging.getLogger("neo4j.bolt").setLevel(logging.WARNING)
    logging.getLogger("neobolt").setLevel(logging.WARNING)
    # silence GitPython debug output
    logging.getLogger("git").setLevel(logging.WARNING)
    logging.getLogger("git.cmd").setLevel(logging.WARNING)
    logging.getLogger("git.repo").setLevel(logging.WARNING)


def capture_main(args):
    vm_name = args['<vm_name>']
    uri = args['--connection']
    debug = args['--debug']
    hooks_config_path = args['<plugins_configuration>']

    init_logger(debug)

    # load hooks.json
    hooks_config = {}
    with open(hooks_config_path) as f:
        try:
            hooks_config = json.load(f)
        except json.JSONDecodeError:
            logging.error("Failed to parse %s: Invalid JSON", hooks_config_path)
            return

    if 'configuration' not in hooks_config:
        hooks_config['configuration'] = {}

    # use default desktop ready delay if unset
    if "desktop_ready_delay" not in hooks_config['configuration']:
        hooks_config['configuration']['desktop_ready_delay'] = DESKTOP_READY_DELAY

    # insert vm_name object
    hooks_config['configuration']['domain_name'] = vm_name
    # insert debug flag
    hooks_config['configuration']['debug'] = debug

    # Neo4j required ?
    neo4j = hooks_config['configuration'].get('neo4j', {})
    if neo4j.get('enabled'):
        logging.info('Connect to Neo4j DB')
        graph = Graph(password=DB_PASSWORD)
        # handle 'delete' key
        # delete entire graph ?
        delete = neo4j.get('delete')
        if delete:
            logging.info("Deleting all nodes in graph database")
            graph.delete_all()
        # handle 'replace' key
        # replace existing OS in Neo4j ?
        replace = neo4j.get('replace', False)
        os_match = OS.match(graph).where("_.name = '{}'".format(vm_name))
        if not replace and os_match.first():
            logging.info('OS %s already inserted, exiting', vm_name)
            return
        elif os_match.first():
            # replace = True and an OS already exists
            logging.info('Deleting previous OS %s', vm_name)
            graph.run(SUBGRAPH_DELETE_OS.format(vm_name))

        # init new OS node
        os_node = OS(vm_name)
        # create it already, so transactions will work in hooks
        logging.info('Creating OS node %s', os_node.name)
        graph.create(os_node)
        neo4j['OS'] = os_node
        neo4j['graph'] = graph

    # Run the protocol
    try:
        with QEMUDomainContextFactory(vm_name, uri) as context:
            with Environment(context, hooks_config) as environment:
                logging.info('Capturing %s', vm_name)
                start = timer()
                protocol(environment)
                end = timer()
                delta = timedelta(seconds=end - start)
                # remove microseconds
                duration = str(delta).split('.')[0]
                logging.info('Capture duration: %s', duration)
    except KeyboardInterrupt:
        # cleanup
        if neo4j.get('enabled'):
            logging.warning("SIGINT received")
            logging.info("cleanup: removing OS node")
            graph: Graph = neo4j.get('graph', None)
            os_node: OS = neo4j.get('OS', None)
            if graph is not None and os_node is not None:
                graph.run(SUBGRAPH_DELETE_OS.format(vm_name))

    if neo4j.get('enabled'):
        # push OS node updates
        os_node: OS = neo4j['OS']
        graph: Graph = neo4j['graph']
        graph.push(os_node)
