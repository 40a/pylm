# The difference between connections and services is that connections
# connect, while services bind.
from pylm_ng.components.core import ComponentInbound, zmq_context
from pylm_ng.components.messages_pb2 import PalmMessage, BrokerMessage
from uuid import uuid4
import zmq
import sys


class RepService(ComponentInbound):
    """
    RepService binds to a given socket and returns something.
    """
    def __init__(self, name, listen_address, broker_address="inproc://broker", palm=False,
                 logger=None, messages=sys.maxsize):
        """
        :param name: Name of the service
        :param listen_address: ZMQ socket address to bind to
        :param broker_address: ZMQ socket address of the broker
        :param logger: Logger instance
        :param palm: True if the service gets PALM messages. False if they are binary
        :param messages: Maximum number of messages. Defaults to infinity
        :return:
        """
        super(RepService, self).__init__(
            name,
            listen_address,
            zmq.REP,
            reply=True,
            broker_address=broker_address,
            bind=True,
            palm=palm,
            logger=logger,
            messages=messages
        )


class PullService(ComponentInbound):
    """
    PullService binds to a socket waits for messages from a push-pull queue.
    """
    def __init__(self, name, listen_address, broker_address="inproc://broker", palm=False,
                 logger=None, messages=sys.maxsize):
        """
        :param name: Name of the service
        :param listen_address: ZMQ socket address to bind to
        :param broker_address: ZMQ socket address of the broker
        :param logger: Logger instance
        :param palm: True if service gets PALM messages. False if they are binary
        :param messages: Maximum number of messages. Defaults to infinity.
        :return:
        """
        super(PullService, self).__init__(
            name,
            listen_address=listen_address,
            socket_type=zmq.PULL,
            reply=False,
            broker_address=broker_address,
            bind=True,
            palm=palm,
            logger=logger,
            messages=messages
        )


class PushPullService(object):
    """
    Push-Pull Service to connect to workers
    """
    def __init__(self,
                 name,
                 push_address,
                 pull_address,
                 broker_address="inproc://broker",
                 palm=False,
                 logger=None,
                 cache=None,
                 messages=sys.maxsize):
        """
        :param name: Name of the component
        :param listen_address: ZMQ socket address to listen to
        :param socket_type: ZMQ inbound socket type
        :param reply: True if the listening socket blocks waiting a reply
        :param broker_address: ZMQ socket address for the broker
        :param bind: True if socket has to bind, instead of connect.
        :param palm: True if the message is waiting is a PALM message. False if it is
          just a binary string
        :param logger: Logger instance
        :param cache: Cache for shared data in the server
        :param messages: Maximum number of inbound messages. Defaults to infinity.
        :return:
        """
        self.name = name.encode('utf-8')

        self.push = zmq_context.socket(zmq.PUSH)
        self.pull = zmq_context.socket(zmq.PULL)
        self.push_address = push_address
        self.pull_address = pull_address
        self.push.bind(push_address)
        self.pull.bind(pull_address)

        self.broker = zmq_context.socket(zmq.REQ)
        self.broker.identity = self.name
        self.broker.connect(broker_address)

        self.palm = palm
        self.logger = logger
        self.cache = cache
        self.messages = messages

    def _translate_to_broker(self, message_data):
        """
        Translate the message that the component has got to be digestible by the broker
        :param message_data:
        :return:
        """
        broker_message_key = str(uuid4())
        if self.palm:
            palm_message = PalmMessage()
            palm_message.ParseFromString(message_data)
            payload = palm_message.payload

            # I store the message to get it later when the message is outbound. See that
            # if I am just sending binary messages, I do not need to assign any envelope.
            self.cache.set(broker_message_key, message_data)
        else:
            payload = message_data

        broker_message = BrokerMessage()
        broker_message.key = broker_message_key
        broker_message.payload = payload

        return broker_message.SerializeToString()

    def _translate_from_broker(self, message_data):
        """
        Translate the message that the component gets from the broker to the output format
        :param message_data:
        :return:
        """
        broker_message = BrokerMessage()
        broker_message.ParseFromString(message_data)

        if self.palm:
            message_data = self.cache.get(broker_message.key)
            palm_message = PalmMessage()
            palm_message.ParseFromString(message_data)
            palm_message.payload = broker_message.payload
            message_data = palm_message.SerializeToString()

        else:
            message_data = broker_message.payload

        return message_data

    def scatter(self, message_data):
        """
        To be overriden. Picks a message and returns a generator that multiplies the messages
        to the broker.
        :param message_data:
        :return:
        """
        yield message_data

    def handle_feedback(self, message_data):
        """
        To be overriden. Handles the feedback from the broker
        :param message_data:
        :return:
        """
        pass

    def reply_feedback(self):
        """
        To be overriden. Returns the feedback if the component has to reply.
        :return:
        """
        return b'0'

    def start(self):
        self.logger.info('Launch component {}'.format(self.name))
        initial_broker_message = BrokerMessage()
        initial_broker_message.key = '0'
        initial_broker_message.payload = b'0'
        self.broker.send(initial_broker_message.SerializeToString())

        for i in range(self.messages):
            self.logger.debug('Component {} blocked waiting for broker'.format(self.name))
            # Workers use BrokerMessages, because they want to know the message ID.
            message_data = self.broker.recv()
            self.logger.debug('Got message from broker')
            for scattered in self.scatter(message_data):
                self.push.send(scattered)
                self.handle_feedback(self.pull.recv())

            self.broker.send(self.reply_feedback())

    def cleanup(self):
        self.push.close()
        self.pull.close()
        self.broker.close()
