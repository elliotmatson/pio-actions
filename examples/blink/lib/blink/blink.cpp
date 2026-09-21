#include "blink.h"

Blink::Blink(uint32_t on_ms, uint32_t off_ms, bool start_on)
    : on_ms_(on_ms),
      off_ms_(off_ms),
      last_change_(0),
      transitions_(0),
      state_(start_on) {}

bool Blink::update(uint32_t now_ms) {
  const uint32_t hold = state_ ? on_ms_ : off_ms_;

  // Unsigned subtraction, so the 49.7-day millis() rollover costs at most one
  // interval instead of stalling the blink until the counter catches back up.
  if (static_cast<uint32_t>(now_ms - last_change_) >= hold) {
    state_ = !state_;
    last_change_ = now_ms;
    ++transitions_;
  }
  return state_;
}
