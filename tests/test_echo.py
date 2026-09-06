"""Telling Meraki's own voice apart from the user's.

On speakers the browser's echo cancellation is not enough and Deepgram
transcribes Meraki talking, which would cancel the turn it is in the middle of.
Muting the microphone would stop that by removing barge-in, so instead a
transcript that is contained in what is currently being said is treated as echo.
"""

from __future__ import annotations

from meraki.main import looks_like_echo

SPOKEN = "The trick is to send the first clause before the model finishes."


# --- echo is rejected --------------------------------------------------------


def test_a_fragment_of_the_current_reply_is_echo():
    assert looks_like_echo("the trick is to send", SPOKEN)


def test_echo_is_recognised_despite_punctuation_and_case():
    """Deepgram punctuates and capitalises; the raw strings will not match."""
    assert looks_like_echo("The trick, is to send!", SPOKEN)


def test_the_whole_utterance_coming_back_is_echo():
    assert looks_like_echo(SPOKEN, SPOKEN)


def test_the_tail_of_a_finished_reply_is_still_echo():
    """Audio keeps playing after the last token, and that tail echoes too."""
    assert looks_like_echo("before the model finishes", SPOKEN)


# --- real speech gets through ------------------------------------------------


def test_the_user_saying_something_else_is_not_echo():
    assert not looks_like_echo("actually hang on", SPOKEN)


def test_shared_words_in_a_different_order_are_not_echo():
    """Word overlap alone must not suppress a genuine interruption."""
    assert not looks_like_echo("send the trick", SPOKEN)


def test_nothing_is_echo_before_the_assistant_has_spoken():
    assert not looks_like_echo("hello there", "")


def test_empty_transcripts_are_not_echo():
    assert not looks_like_echo("", SPOKEN)
    assert not looks_like_echo("   ", SPOKEN)


def test_a_question_containing_a_spoken_word_still_interrupts():
    assert not looks_like_echo("wait what model", SPOKEN)
