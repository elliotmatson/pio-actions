#include <Arduino.h>

#include "blink.h"

// Supplied per env in platformio.ini.
#ifndef LED_PIN
#define LED_PIN 2
#endif

// Injected by the build-release workflow via PLATFORMIO_BUILD_FLAGS. The
// defaults keep a plain `pio run` on a laptop working.
#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif
#ifndef FW_TYPE
#define FW_TYPE "local"
#endif
#ifndef REPO_URL
#define REPO_URL "elliotmatson/pio-actions"
#endif

namespace {
Blink led(120, 880);
Blink status(500, 500);
uint32_t last_report = 0;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  Serial.printf("blink %s (%s) from %s\n", FW_VERSION, FW_TYPE, REPO_URL);
}

void loop() {
  const uint32_t now = millis();
  digitalWrite(LED_PIN, led.update(now) ? HIGH : LOW);
  status.update(now);

  // A once-a-second line, so a serial monitor shows the fixture is alive.
  if (now - last_report >= 1000) {
    last_report = now;
    Serial.printf("up %lus led=%d status=%d transitions=%lu\n",
                  static_cast<unsigned long>(now / 1000UL), led.state(),
                  status.state(),
                  static_cast<unsigned long>(led.transitions()));
  }
  delay(1);
}
