#pragma once

#include <stdint.h>

/// A square-wave blink with independent on and off durations.
///
/// Deliberately free of Arduino.h: the same object compiles for the firmware
/// and for the host test runner, which is the whole point of keeping timing
/// logic out of loop().
class Blink {
 public:
  Blink(uint32_t on_ms, uint32_t off_ms, bool start_on = false);

  /// Advance to `now_ms` and return the LED state that should be applied.
  bool update(uint32_t now_ms);

  bool state() const { return state_; }
  uint32_t transitions() const { return transitions_; }

 private:
  uint32_t on_ms_;
  uint32_t off_ms_;
  uint32_t last_change_;
  uint32_t transitions_;
  bool state_;
};
