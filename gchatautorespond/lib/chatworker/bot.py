import datetime
import logging
import re
import ssl
import threading

from django.core.mail import EmailMessage
from raven.processors import Processor
from sleekxmpp import ClientXMPP
from sleekxmpp.xmlstream import cert

from gchatautorespond.lib import report_ga_event_async

# This should be under 10 characters to avoid Google truncating it.
# See https://github.com/simon-weber/gchatautorespond/issues/3.
RESOURCE = 'autore'


class ContextFilter(logging.Filter):
    """If context.log_id is set, add it to log messages and tags.

    This separates sleekxmpp logs by bot.
    """

    context = threading.local()

    @classmethod
    def filter(cls, record):
        if getattr(cls.context, 'log_id', None):
            record.msg = "[log_id=%s] %s" % (cls.context.log_id, record.msg)
            record.tags = getattr(record, 'tags', {})
            record.tags['log_id'] = cls.context.log_id
        return True


class ContextProcessor(Processor):
    """Remove the prepended log_id bit from messages going to sentry, since it prevents aggregation.

    Sentry gets this information from the tags instead.
    """

    pattern = re.compile(r'^\[log_id=\d\d*\] ')

    def get_data(self, data, **kwargs):

        if 'message' in data:
            data['message'] = re.sub(self.pattern, '', data['message'])

        if 'sentry.interfaces.Message' in data:
            imes = data['sentry.interfaces.Message']
            if 'message' in imes:
                imes['message'] = re.sub(self.pattern, '', imes['message'])
            if 'formatted' in imes:
                imes['formatted'] = re.sub(self.pattern, '', imes['formatted'])

        return data


class GChatBot(ClientXMPP):
    """A Bot that connects to Google Chat over ssl."""

    def __init__(self, email, token, log_id, **kwargs):
        """
        Args:
            email (unicode): the email to login as, including domain.
              Custom domains are supported.
            token (string): a `googletalk` scoped oauth2 access token
            log_id: if not None, will be prepended onto all logging messages triggered by this bot.
        """

        if '@' not in email:
            raise ValueError('email must be a full email')

        super(GChatBot, self).__init__(email + '/' + RESOURCE, token)

        self.email = email
        self.log_id = log_id

        logger_name = __name__
        if self.log_id is not None:
            logger_name += str(self.log_id)

        self.logger = logging.getLogger(logger_name)
        self.logger.info("bot initialized (%s)", id(self))

        self.use_ipv6 = False
        self.auto_reconnect = True

        self.add_event_handler('session_start', self.session_start)
        self.add_event_handler('ssl_invalid_cert', self.ssl_invalid_cert)

    # Since python doesn't have inheritable threadlocals, we need to set the context from one spot in each new thread.
    # These spots were found from http://sleekxmpp.com/_modules/sleekxmpp/xmlstream/xmlstream.html#XMLStream.process.
    # It doesn't include the scheduler thread, but that doesn't seem to log anything interesting.
    def _process(self, *args, **kwargs):
        if self.log_id:
            ContextFilter.context.log_id = self.log_id
        super(GChatBot, self)._process(*args, **kwargs)

    def _send_thread(self, *args, **kwargs):
        if self.log_id:
            ContextFilter.context.log_id = self.log_id
        super(GChatBot, self)._send_thread(*args, **kwargs)

    def _event_runner(self, *args, **kwargs):
        if self.log_id:
            ContextFilter.context.log_id = self.log_id
        super(GChatBot, self)._event_runner(*args, **kwargs)

    def connect(self):
        self.credentials['api_key'] = self.boundjid.bare
        self.credentials['access_token'] = self.password
        super(GChatBot, self).connect(('talk.google.com', 5222))

    def session_start(self, event):
        # TODO try seeing if send_presence will trigger presence responses for use in autodetect
        self.send_presence()
        self.get_roster()

        # Most get_*/set_* methods from plugins use Iq stanzas, which
        # can generate IqError and IqTimeout exceptions. Example code:
        #
        # try:
        #     self.get_roster()
        # except IqError as err:
        #     logging.error('There was an error getting the roster')
        #     logging.error(err.iq['error']['condition'])
        #     self.disconnect()
        # except IqTimeout:
        #     logging.error('Server is taking too long to respond')
        #     self.disconnect()

    def ssl_invalid_cert(self, pem_cert):
        # Source: https://github.com/poezio/slixmpp/blob/master/examples/gtalk_custom_domain.py

        der_cert = ssl.PEM_cert_to_DER_cert(pem_cert)
        try:
            cert.verify('talk.google.com', der_cert)
            self.logger.info("found GTalk certificate")
        except cert.CertificateError as err:
            self.logger.error(err.message)
            self.disconnect(send_close=False)


class AutoRespondBot(GChatBot):
    """A GChatBot that responds to incoming messages with a set response.

    This works for all sender/receiver combinations ({gchat, hangouts} x {gchat, hangouts}).

    Hangouts messages are sent over a Google-internal xmpp bridge.
    They can mostly be treated normally, with two exceptions:

      * Hangouts senders have weird jids and don't reveal their email. This isn't
        a huge problem because we get their full name through the roster.
      * the body of Hangouts invites is never seen. This might be a bug? Or just something
        Google didn't want to build an extension for? Either way, this situation
        usually resolves itself, since we'll respond to the first message in the new conversation.

    There is a way to respond to chat invites, but it seems to be more trouble than it's worth.
    It involves listening for:
      * a roster subscription request from a Hangouts jid
      * later, a resource under that jid coming online
    """

    def __init__(self, email, token, log_id, response, notify_email,
                 response_throttle=datetime.timedelta(seconds=60), detect_unavailable=True):
        """
        Args:
            email (string): see GChatBot.
            token (string): see GChatBot.
            response (string): the message to respond with.
            notify_email (string): if not None, an email will be sent for each response.
            response_throttle (datetime.timedelta): no more than one response will be sent during this interval.
            detect_unavailable (bool): when True, don't autorespond if another resource for the same account is
              available and not away.
        """

        self.response = response
        self.notify_email = notify_email
        self.response_throttle = response_throttle
        self.detect_unavailable = detect_unavailable

        # FIXME this never gets cleaned up, leaking (a small amount of) memory
        self.last_reply_datetime = {}  # {jid: datetime.datetime}
        self.other_active_resources = set()  # jids of other resources for our user

        super(AutoRespondBot, self).__init__(email, token, log_id)

        self.add_event_handler('message', self.message)
        self.add_event_handler('presence', self.presence)

        # uncomment this to respond to chat invites.
        # self.add_event_handler('roster_subscription_request',
        #                        self.roster_subscription_request)
        # self.add_event_handler('presence_available', self.detect_hangouts_jids)

        self.hangouts_jids_seen = set()

    def message(self, msg):
        """Respond to Hangouts/gchat normal messages."""

        self.logger.info("received message from %s: %r", msg['from'], msg)
        if msg['type'] in ('chat', 'normal') and self._should_send_to(msg['from']):
            jid = msg['from']
            body = msg.get('body')
            self.logger.info("responding to %s via message. message %r", jid, msg)
            msg.reply(self.response).send()
            self._sent_reply(jid, body)

    def presence(self, presence):
        other_jid = presence['from']
        if other_jid.bare == self.boundjid.bare:  # only compare the user+domain
            if other_jid.resource.startswith(RESOURCE):
                # There's probably something more to be done here, like ensuring only one autoresponder replies
                # (maybe the one with the highest resource?).
                # For now, they're not considered another resource, and multiple bots can respond.
                self.logger.error('more than one autoresponder is running? we are %s and they are %s',
                                  self.boundjid, other_jid)
                return
            if other_jid == self.boundjid:
                self.logger.error('received a loopback message not caught by resource! us: %r them: %r',
                                  self.boundjid, other_jid)
                return

            if presence['type'] == 'available':
                self.other_active_resources.add(other_jid)
                self.logger.info('other resource came online: %s', other_jid)
            elif presence['type'] in ('away', 'dnd', 'unavailable'):
                self.other_active_resources.discard(other_jid)
                self.logger.info('other resource %s now %s', other_jid, presence['type'])

    def roster_subscription_request(self, presence):
        """Watch for Hangouts bridge chat invites and add them to `hangouts_jids_seen`."""

        from_jid = presence['from']

        if from_jid.domain == 'public.talk.google.com':
            # Hangouts users get these goofy jids.
            # Replying to them doesn't seem to work, but replying to resources under it will.
            # So, we store the bare jid, with a weird name thing stripped out, then
            # wait for a resource to become active.
            if '--' in from_jid.user:
                waiting_jid = from_jid.bare.partition('--')[-1]
            else:
                waiting_jid = from_jid.bare

            self.logger.info("saw hangouts jid %s. message %r", from_jid, presence)
            self.hangouts_jids_seen.add(waiting_jid)

    def detect_hangouts_jids(self, presence):
        """Watch for Hangouts bridge jids coming online and respond to any in `hangouts_jids_seen`."""

        from_jid = presence['from']
        if from_jid.bare in self.hangouts_jids_seen and from_jid.resource:
            self.hangouts_jids_seen.remove(from_jid.bare)
            if self._should_send_to(from_jid):
                # Message type is important; omitting it will silently discard the message.
                self.logger.info("responding to %s via presence. message %r", from_jid, presence)
                self.send_message(mto=from_jid, mbody=self.response, mtype='chat')
                self._sent_reply(from_jid)

    def _sent_reply(self, jid, message=None):
        """Perform any bookkeeping needed after a response is sent.

        Args:
            jid: the jid that was responded to.
            message (string): the message received. None if unknown.
        """

        self.last_reply_datetime[jid] = datetime.datetime.now()

        report_ga_event_async(self.email, category='message', action='receive')

        if self.notify_email is not None:
            from_identifier = jid.jid
            from_nick = self.client_roster[jid.jid]['name']
            if from_nick:
                from_identifier = "%s (%s)" % (from_nick, jid.jid)

            body_paragraphs = ["gchat.simon.codes just responded to a message from %s." % from_identifier]

            if message is not None:
                body_paragraphs.append("The message we received was \"%s\"." % message.encode('utf-8'))
            else:
                body_paragraphs.append("Due to a bug on Google's end, we didn't receive a message body.")

            body_paragraphs.append("We replied with your autoresponse \"%s\"." % self.response.encode('utf-8'))

            body_paragraphs.append("If any of this is unexpected or strange, email support@gchat.simon.codes for support.")

            email = EmailMessage(
                subject='gchat.simon.codes sent an autoresponse',
                to=[self.notify_email],
                body='\n\n'.join(body_paragraphs),
                reply_to=['noreply@gchat.simon.codes'],
            )
            email.send(fail_silently=True)
            self.logger.info("sent an email notification to %r", self.notify_email)

    def _should_send_to(self, jid):
        """Return False if another resource is active or messages to the given jid are throttled."""

        throttled = False
        if jid in self.last_reply_datetime:
            throttled = (datetime.datetime.now() - self.last_reply_datetime[jid]) < self.response_throttle

        if throttled:
            self.logger.info("do not send; bot is throttled")
            return False

        if self.detect_unavailable:
            # Ideally we could check the status the other resources right now to make sure they're still active.
            # However, Google doesn't seem to respond to presence probes, and pings seem to always come back.
            if self.other_active_resources:
                self.logger.info('do not send; other resources are active: %s', self.other_active_resources)
                return False

        return True
