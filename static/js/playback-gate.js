/**
 * Decides when the UI may leave "Speaking".
 *
 * Two things must both be true: the server has said the turn's audio is all
 * sent (`speech_done`), and the player has nothing left to play. They arrive in
 * either order - `speech_done` lands in the same burst as the last chunk, well
 * before that chunk has finished decoding, let alone playing - so neither alone
 * is enough. Letting the player's idle signal settle the state by itself would
 * flip it in every gap between chunks of one reply.
 */

export class PlaybackGate {
  constructor({ isPlaying, onSettled }) {
    this.isPlaying = isPlaying;
    this.onSettled = onSettled;
    this.turnOpen = false;
    // Set when a turn ends, spent when it settles. Without it a stray idle
    // signal after an interrupt would settle a turn that was already handled.
    this.awaitingSettle = false;
    // True once the current turn has had its end, and before any turn exists,
    // so an end with no turn behind it is ignored. Deliberately not tied to
    // cancel(): clearing the conversation cancels the gate while the server's
    // turn carries on, and that turn's end must still settle.
    this.ended = true;
  }

  /** A turn began; nothing settles until it ends. */
  turnStarted() {
    this.turnOpen = true;
    this.awaitingSettle = false;
    this.ended = false;
  }

  /** The server has sent every chunk for this turn. */
  turnEnded() {
    // A failed synthesis sends error(tts) and then speech_done for one turn;
    // the second must not settle a turn that has already settled.
    if (this.ended) return;
    this.ended = true;
    this.turnOpen = false;
    this.awaitingSettle = true;
    this.check();
  }

  /** The player ran out of audio. */
  playerIdle() {
    this.check();
  }

  /** Interrupted or failed: the caller sets the state itself. */
  cancel() {
    this.turnOpen = false;
    this.awaitingSettle = false;
  }

  check() {
    if (!this.awaitingSettle || this.isPlaying()) return;
    this.awaitingSettle = false;
    this.onSettled();
  }
}
