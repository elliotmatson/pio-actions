#include <unity.h>

#include "blink.h"

void setUp() {}
void tearDown() {}

static void starts_off_and_holds_through_the_off_period() {
  Blink b(100, 200);
  TEST_ASSERT_FALSE(b.update(0));
  TEST_ASSERT_FALSE(b.update(199));
  TEST_ASSERT_EQUAL_UINT32(0, b.transitions());
}

static void turns_on_once_the_off_period_elapses() {
  Blink b(100, 200);
  b.update(0);
  TEST_ASSERT_TRUE(b.update(200));
  TEST_ASSERT_EQUAL_UINT32(1, b.transitions());
}

static void on_and_off_durations_are_independent() {
  Blink b(100, 200);
  b.update(200);   // -> on
  TEST_ASSERT_TRUE(b.update(299));
  TEST_ASSERT_FALSE(b.update(300));  // on period is the shorter 100 ms
}

// The reason this logic is worth extracting and testing at all: a naive
// `now > last + interval` stalls for 49.7 days after the counter wraps.
static void survives_the_millis_rollover() {
  Blink b(100, 100);
  TEST_ASSERT_TRUE(b.update(0xFFFFFF9Cu));  // 100 ms short of the wrap
  TEST_ASSERT_TRUE(b.update(0xFFFFFFF0u));  // only 84 ms later: no change yet
  TEST_ASSERT_FALSE(b.update(0x00000000u)); // wrapped, but exactly 100 ms on
  TEST_ASSERT_EQUAL_UINT32(2, b.transitions());
}

static void a_full_cycle_counts_two_transitions() {
  Blink b(100, 100);
  uint32_t now = 0;
  for (int i = 0; i < 20; ++i) {
    now += 100;
    b.update(now);
  }
  TEST_ASSERT_EQUAL_UINT32(20, b.transitions());
}

int main(int, char **) {
  UNITY_BEGIN();
  RUN_TEST(starts_off_and_holds_through_the_off_period);
  RUN_TEST(turns_on_once_the_off_period_elapses);
  RUN_TEST(on_and_off_durations_are_independent);
  RUN_TEST(survives_the_millis_rollover);
  RUN_TEST(a_full_cycle_counts_two_transitions);
  return UNITY_END();
}
