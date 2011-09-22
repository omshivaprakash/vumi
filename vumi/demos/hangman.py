# -*- test-case-name: vumi.demos.tests.test_hangman -*-

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import log

from vumi.application import ApplicationWorker
from vumi.utils import (safe_routing_key, get_deploy_int,
                        http_request, normalize_msisdn)

import redis
import string


class HangmanGame(object):
    """Represents a game of Hangman.

       Parameters
       ----------
       word : str
           Word to guess.
       guesses : set, optional
           Characters guessed so far. If None, defaults to the empty set.
       msg : str, optional
           Message set in reply to last user action. Defaults to 'New game!'.
       """

    UI_TEMPLATE = \
        u"%(msg)s\n" \
        u"Word: %(word)s\n" \
        u"Letters guessed so far: %(guesses)s\n" \
        u"%(prompt)s (0 to quit):\n"

    # exit codes
    NOT_DONE, DONE, DONE_WANTS_NEW = range(3)

    def __init__(self, word, guesses=None, msg="New game!"):
        self.word = word
        self.guesses = guesses if guesses is not None else set()
        self.msg = msg
        self.exit_code = self.NOT_DONE

    def state(self):
        """Serialize the Hangman object to a string."""
        guesses = u"".join(sorted(self.guesses))
        state = u"%s:%s:%s" % (self.word, guesses, self.msg)
        return state.encode("utf-8")

    @classmethod
    def from_state(cls, state):
        state = state.decode("utf-8")
        word, guesses, msg = state.split(":", 2)
        guesses = set(guesses)
        return cls(word=word, guesses=guesses, msg=msg)

    def event(self, message):
        """Handle an user input string.

           Parameters
           ----------
           message : unicode
               Message received from user.
           """
        message = message.lower()
        if not message:
            self.msg = u"Some input required please."
        elif len(message) > 1:
            self.msg = u"Single characters only please."
        elif message == '0':
            self.exit_code = self.DONE
            self.msg = u"Game ended."
        elif self.won():
            self.exit_code = self.DONE_WANTS_NEW
        elif message not in string.lowercase:
            self.msg = u"Letters of the alphabet only please."
        elif message in self.guesses:
            self.msg = u"You've already guessed '%s'." % (message,)
        else:
            assert len(message) == 1
            self.guesses.add(message)
            log.msg("Message: %r, word: %r" % (message, self.word))
            if message in self.word:
                self.msg = u"Word contains at least one '%s'! :D" % (message,)
            else:
                self.msg = u"Word contains no '%s'. :(" % (message,)

        if self.won():
            self.msg = self.victory_message()

    def victory_message(self):
        uniques = len(set(self.word))
        guesses = len(self.guesses)
        for factor, msg in [
            (1, u"Flawless victory!"),
            (1.5, u"Epic victory!"),
            (2, u"Standard victory!"),
            (3, u"Sub-par victory!"),
            (4, u"Random victory!"),
            ]:
            if guesses <= uniques * factor:
                return msg
        return u"Button mashing!"

    def won(self):
        return all(x in self.guesses for x in self.word)

    def draw_board(self):
        """Return a text-based UI."""
        if self.exit_code != self.NOT_DONE:
            return u"Adieu!"
        word = u"".join((x if x in self.guesses else '_') for x in self.word)
        guesses = "".join(sorted(self.guesses))
        if self.won():
            prompt = u"Enter anything to start a new game"
        else:
            prompt = u"Enter next guess"
        return self.UI_TEMPLATE % {'word': word,
                                   'guesses': guesses,
                                   'msg': self.msg,
                                   'prompt': prompt,
                                   }


class HangmanWorker(ApplicationWorker):
    """Worker that plays Hangman.

       Configuration
       -------------
       transport_name : str
           Name of the transport.
       ussd_code : str
           USSD code.
       random_word_url : URL
           Page to GET a random word from.
           E.g. http://randomword.setgetgo.com/get.php
       """

    @inlineCallbacks
    def startWorker(self):
        """Start the worker"""
        # Connect to Redis
        self.r_server = redis.Redis("localhost",
                                    db=get_deploy_int(self._amqp_client.vhost))
        log.msg("Connected to Redis")
        self.r_prefix = "hangman:%s:%s" % (
                self.config['transport_name'],
                safe_routing_key(self.config['ussd_code']))
        log.msg("r_prefix = %s" % self.r_prefix)
        self.random_word_url = self.config['random_word_url']
        log.msg("random_word_url = %s" % self.random_word_url)

        yield super(HangmanWorker, self).startWorker()

    def random_word(self):
        log.msg('Fetching random word from %s' % (self.random_word_url,))
        d = http_request(self.random_word_url, None, method='GET')

        def _decode(word):
            # result from http_request should always be bytes
            # convert to unicode, strip BOMs and whitespace
            word = word.decode("utf-8", "ignore")
            word = word.lstrip(u'\ufeff\ufffe')
            word = word.strip()
            return word
        return d.addCallback(_decode)

    def game_key(self, msisdn):
        "Key for looking up a users game in data store."""
        msisdn = normalize_msisdn(msisdn)
        userid = msisdn.lstrip('+')
        return "%s#%s" % (self.r_prefix, userid)

    def load_game(self, msisdn):
        """Fetch a game for the given user ID.
           """
        game_key = self.game_key(msisdn)
        state = self.r_server.get(game_key)
        if state is not None:
            game = HangmanGame.from_state(state)
        else:
            game = None
        return game

    @inlineCallbacks
    def new_game(self, msisdn):
        """Create a new game for the given user ID.
           """
        word = yield self.random_word()
        word = word.strip().lower()
        game = HangmanGame(word)
        returnValue(game)

    def save_game(self, msisdn, game):
        """Save the game state for the given game."""
        game_key = self.game_key(msisdn)
        state = game.state()
        self.r_server.set(game_key, state)

    def delete_game(self, msisdn):
        """Delete the users saved game."""
        game_key = self.game_key(msisdn)
        self.r_server.delete(game_key)

    @inlineCallbacks
    def new_session(self, data):
        """Find or creating hangman game for this player.

           Sends current state.
           """
        log.msg("New session:", data)
        session_id = data['transport_session_id']
        msisdn = data['sender']
        game = self.load_game(msisdn)
        if game is None:
            game = yield self.new_game(msisdn)
            self.save_game(msisdn, game)
        self.reply(session_id, game.draw_board())

    def close_session(self, data):
        # Hangman games intentionally stick around
        # and can be picked up again later.
        pass

    @inlineCallbacks
    def resume_session(self, data):
        log.msg("Resume session:", data)
        session_id = data['transport_session_id']
        msisdn = data['sender']
        message = data['message'].strip()
        game = self.load_game(msisdn)
        game.event(message)
        if game.exit_code == game.DONE:
            self.delete_game(msisdn)
            self.end(session_id, game.draw_board())
        elif game.exit_code == game.DONE_WANTS_NEW:
            game = yield self.new_game(msisdn)
            self.save_game(msisdn, game)
            self.reply(session_id, game.draw_board())
        else:
            self.save_game(msisdn, game)
            self.reply(session_id, game.draw_board())
