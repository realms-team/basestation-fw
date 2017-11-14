from connector import Connector
import threading
import logging
import time

logger = logging.getLogger(__name__)

# we buffer objects for BUFFER_PERIOD second to ensure they are written to
# file chronologically
BUFFER_PERIOD = 30


class ConnectorFile(Connector):

    def _start(self):
        # create file if it does not exists
        open(self.host, 'a+')

        if self.pubrate_min != 0:
            # start pubthread
            self._publish_task()

    def publish(self, msg, topic=None):
        """
        Publish a message
        :param dict msg: the message to send
        :param string topic: the topic to send to
        """

        # if pubrate_min == 0, send now
        if self.pubrate_min == 0:
            self._publish_now([msg])

        # else, add message to queue
        else:
            self.publish_queue.append(msg)

    def _publish_now(self, msg_list, topic=None):
        logger.debug("publishing now")
        self.sol.dumpToFile(
            msg_list,
            self.host,
        )

    def _publish_task(self):
        if len(self.publish_queue) > 0:
            with self.queue_lock:
                # order solJsonObjectsToPublish chronologically
                self.publish_queue.sort(key=lambda i: i['timestamp'])

                # extract the JSON SOL objects heard more than BUFFER_PERIOD ago
                now = time.time()
                solJsonObjectsToWrite = []
                while True:
                    if not self.publish_queue:
                        break
                    if now - self.publish_queue[0]['timestamp'] < BUFFER_PERIOD:
                        break

                solJsonObjectsToWrite += [self.publish_queue.pop(0)]

            self._publish_now(solJsonObjectsToWrite)

        # restart after pubrate_min
        threading.Timer(self.pubrate_min * 60, self._publish_task).start()
