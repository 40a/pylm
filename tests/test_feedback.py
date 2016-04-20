from threading import Thread
from pylm_ng.components.core import zmq_context
from pylm_ng.components.messages_pb2 import BrokerMessage
from pylm_ng.components.endpoints import logger
from uuid import uuid4
import zmq

# To be deleted
import sys


# Temporally copied here
class Broker(object):
    """
    Broker for the internal event-loop. It is a ROUTER socket that blocks
    waiting for the components to send something. This is more a bus than
    a broker.
    """
    def __init__(self,
                 inbound_address="inproc://inbound",
                 outbound_address="inproc://outbound",
                 logger=None,
                 cache=None,
                 messages=sys.maxsize):
        """
        Initiate a broker instance
        :param inbound_address: Valid ZMQ bind address for inbound components
        :param outbound_address: Valid ZMQ bind address for outbound components
        :param logger: Logger instance
        :param cache: Global cache of the server
        :param messages: Maximum number of inbound messages. Defaults to infinity.
        :param messages: Number of messages allowed before the router starts buffering.
        :return:
        """
        # Socket that will listen to the inbound components
        self.inbound = zmq_context.socket(zmq.ROUTER)
        self.inbound.bind(inbound_address)
        self.inbound_address = inbound_address
        self.inbound_components = {}

        # Socket that listens to the outbound components
        self.outbound = zmq_context.socket(zmq.ROUTER)
        self.outbound.bind(outbound_address)
        self.outbound_address = outbound_address
        self.outbound_components = {}

        # Other utilities
        self.logger = logger
        self.messages = messages

        # Ledger for inbound components that are waiting for a message
        self.ledger = {}

        # Despite this is a dictionary, only one message can be buffered
        self.buffer = {}

        # Poller for the event loop
        self.poller = zmq.Poller()
        # Register only inbound to avoid having to buffer stuff
        self.poller.register(self.outbound, zmq.POLLIN)

        # Cache for the server
        self.cache = cache

    def register_inbound(self, name, route='', block=False, log=''):
        """
        Register component by name.
        :param name: Name of the component. Each component has a name, that
          uniquely identifies it to the broker
        :param route: Each message that the broker gets from the component
          may be routed to another component. This argument gives the name
          of the target component for the message.
        :param block: Register if the component is waiting for a reply.
        :param log: Log message for each inbound connection.
        :return:
        """
        self.inbound_components[name.encode('utf-8')] = {
            'route': route.encode('utf-8'),
            'block': block,
            'log': log
        }

    def register_outbound(self, name, log=''):
        self.outbound_components[name.encode('utf-8')] = {
            'log': log
        }

    def start(self):
        # List of available outbound components
        available_outbound = []

        self.logger.info('Launch broker')
        self.logger.info('Inbound socket: {}'.format(self.inbound))
        self.logger.info('Outbound socket: {}'.format(self.outbound))

        for i in range(self.messages):
            # Polls the outbound socket for inbound and outbound connections
            event = dict(self.poller.poll())
            self.logger.debug('Event {}'.format(event))

            if self.outbound in event:
                component, empty, feedback = self.outbound.recv_multipart()
                broker_message = BrokerMessage()
                broker_message.ParseFromString(feedback)
                self.logger.debug('Handling outbound event: {}'.format(broker_message.key))

                # If any message has been buffered,
                if component in self.buffer:
                    self.logger.debug('The message was buffered')
                    message_data = self.buffer.pop(component)
                    self.outbound.send_multipart([component, empty, message_data])
                else:
                    self.logger.debug('Component added as available')
                    available_outbound.append(component)

                # Check if some socket is waiting for the response.
                if broker_message.key in self.ledger:
                    self.logger.debug('Message ID found in ledger')
                    component = self.ledger.pop(broker_message.key)
                    self.logger.debug('Unblocking pending inbound: {}'.format(component))
                    self.inbound.send_multipart([component, empty, broker_message.payload])

                # TODO: Check if the ledger condition is necessary.
                if available_outbound and not self.buffer and not self.ledger:
                    self.poller.register(self.inbound, zmq.POLLIN)

            elif self.inbound in event:
                broker_message = BrokerMessage()
                self.logger.debug('Handling inbound event')
                component, empty, message_data = self.inbound.recv_multipart()
                broker_message.ParseFromString(message_data)

                # Start internal routing
                route_to = self.inbound_components[component]['route']
                block = self.inbound_components[component]['block']

                # If no routing needed
                if not route_to:
                    self.logger.debug('Message back to {}'.format(route_to))
                    self.inbound.send_multipart([component, empty, b'1'])

                # If an outbound is listening
                elif route_to in available_outbound:
                    self.logger.debug('Broker routing to {}'.format(route_to))
                    available_outbound.remove(route_to)
                    self.outbound.send_multipart([route_to, empty, broker_message.SerializeToString()])

                    # Unblock the component
                    if not block:
                        self.logger.debug('Unblocking inbound')
                        self.inbound.send_multipart([component, empty, b'1'])
                    else:
                        self.ledger[broker_message.key] = component
                        self.logger.debug('Inbound waiting for feedback: {}'.format(self.ledger))
                        # TODO: Check if ledger condition is necessary
                        self.poller.unregister(self.inbound)

                # If the corresponding outbound not is listening, buffer the message
                else:
                    self.buffer[route_to] = broker_message.SerializeToString()
                    self.logger.info('Sent to buffer.')
                    self.poller.unregister(self.inbound)

            else:
                self.logger.critical('Socket not known.')

            self.logger.debug('Finished event cycle.')


def inbound1(listen_addr):
    broker = zmq_context.socket(zmq.REQ)
    broker.identity = b'inbound1'
    broker.connect(listen_addr)

    for i in range(0, 10, 2):
        broker_message = BrokerMessage()
        broker_message.key = str(uuid4()).encode('utf-8')
        broker_message.payload = str(i).encode('utf-8')
        broker.send(broker_message.SerializeToString())
        returned = broker.recv()
        assert int(returned) == i

    print('DEBUG: Inbound 1 finished')


def inbound2(listen_addr):
    broker = zmq_context.socket(zmq.REQ)
    broker.identity = b'inbound2'
    broker.connect(listen_addr)

    for i in range(1, 10, 2):
        broker_message = BrokerMessage()
        broker_message.key = str(uuid4()).encode('utf-8')
        broker_message.payload = str(i).encode('utf-8')
        broker.send(broker_message.SerializeToString())
        returned = broker.recv()
        assert int(returned) == 1

    print('DEBUG: Inbound 2 finished')


def outbound(listen_addr):
    broker = zmq_context.socket(zmq.REQ)
    broker.identity = b'outbound'
    broker.connect(listen_addr)
    broker_message = BrokerMessage()
    broker_message.key = '0'
    broker_message.payload = b'READY'
    broker.send(broker_message.SerializeToString())

    for i in range(10):
        broker_message.ParseFromString(broker.recv())
        broker.send(broker_message.SerializeToString())

    print('DEBUG: Outbound finished')


def test_feedback():
    broker = Broker(logger=logger, messages=20)
    broker.register_inbound('inbound1', route='outbound', block=True, log='inbound1')
    broker.register_inbound('inbound2', route='outbound', log='inbound2')
    broker.register_inbound('outbound', log='outbound')

    threads = [
        Thread(target=broker.start),
        Thread(target=inbound1, args=(broker.inbound_address,)),
        Thread(target=inbound2, args=(broker.inbound_address,)),
        Thread(target=outbound, args=(broker.outbound_address,))
        ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    print('DEBUG: Test completed...')

if __name__ == '__main__':
    test_feedback()