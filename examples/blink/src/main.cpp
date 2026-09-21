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
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  Serial.printf("blink %s (%s) from %s\n", FW_VERSION, FW_TYPE, REPO_URL);
}

void loop() {
  digitalWrite(LED_PIN, led.update(millis()) ? HIGH : LOW);
  delay(1);
}
