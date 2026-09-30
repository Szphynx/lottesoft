/*
  GlobalXY stripe + 16-pin output + live UDP control — Teensy 4.1

  Works with blink_control.py unchanged. The number you send (70..2000)
  now drives two things at once:

      70   fast stripe, cool blue
      2000 slow stripe, warm amber

  Everything in between interpolates both at once, so one slider gives you
  a quick feel for how speed and colour temperature read on the fans.

  Three differences from the original sketch:

    1. Sixteen output pins, one per 96-LED panel. 16 panels x 6 fans = 96
       fans. The pin order is the FlexIO parallel group for the Teensy 4.1.

    2. The XYTable entry at row 7, column 2 was 164 — out of the 0..95
       range a panel can hold, so it wrote into the next panel's LEDs.
       Corrected to 65535 (no LED at that cell).

    3. No delay() anywhere, and UDP is drained every pass, so the link
       keeps working while the animation runs.

    4. Accepts whole LED frames from fan_wall.py on the same port (packets
       starting with 'F'). While frames arrive the stripe pauses; it comes
       back 1 s after they stop. fan_wall.py reads this file's XYTable and
       k* constants for the wall layout -- change the wiring here, it follows.

  Needs NativeEthernet (ships with Teensyduino) and FastLED.
*/

#include <NativeEthernet.h>
#include <NativeEthernetUdp.h>
#include <FastLED.h>

// ---- network: same addresses as the blink test -----------------------------
IPAddress teensyIP(192, 168, 60, 50);
IPAddress subnetMask(255, 255, 255, 0);
IPAddress gatewayIP(192, 168, 60, 1);   // nothing there; never consulted
IPAddress dnsIP(192, 168, 60, 1);       // same, unused
const uint16_t LISTEN_PORT = 5005;

byte mac[6];
EthernetUDP udp;
char packetBuf[64];
char replyBuf[96];

// ---- matrix ----------------------------------------------------------------
#define COLOR_ORDER GRB
#define CHIPSET     WS2812B

const uint8_t kMatrixWidth  = 14;   // cells across one panel
const uint8_t kMatrixHeight = 21;   // cells down one panel
const uint8_t kCols = 4;            // panels across
const uint8_t kRows = 4;            // panels down
const uint8_t kTotalPanels = kRows * kCols;          // 16, one per pin

#define kLEDS_PER_PANEL 96
#define NUM_LEDS (kLEDS_PER_PANEL * kTotalPanels)    // 1536
#define OUT_OF_BOUNDS (NUM_LEDS)

const uint16_t GRID_W = kMatrixWidth * kCols;        // 56 cells
const uint16_t GRID_H = kMatrixHeight * kRows;       // 84 cells

CRGB leds[NUM_LEDS];

// ---- control ---------------------------------------------------------------
const uint32_t MIN_CTRL = 70;
const uint32_t MAX_CTRL = 2000;

uint32_t control = 500;        // what the Python side last sent
uint32_t moveInterval = 60;    // ms between stripe steps, derived from control
CRGB stripeColor;              // derived from control
CRGB bgColor;                  // derived from control

uint32_t lastMove = 0;
int16_t yPos = 0;
int8_t yDir = 1;

// Heartbeat on the onboard LED, so you can see the sketch is alive even
// with no fans connected.
uint32_t lastBeat = 0;
bool beatState = false;

// ---- panel mapping ---------------------------------------------------------

uint16_t XYPanel(uint8_t panelCol, uint8_t panelRow, uint8_t x, uint8_t y) {
  if ((x >= kMatrixWidth) || (y >= kMatrixHeight)) {
    return OUT_OF_BOUNDS;
  }

  uint16_t panelNum = panelRow * kCols + panelCol;

  static const uint16_t XYTable[] = {
    65535, 65535, 65535,     8, 65535, 65535, 65535, 65535, 65535, 65535,    56, 65535, 65535, 65535,
    65535, 65535,     9, 65535,     7, 65535, 65535, 65535, 65535,    57, 65535,    55, 65535, 65535,
    65535,    10, 65535,     2, 65535,     6, 65535, 65535,    58, 65535,    50, 65535,    54, 65535,
       11, 65535,     3, 65535,     1, 65535,     5,    59, 65535,    51, 65535,    49, 65535,    53,
    65535,    12, 65535,     0, 65535,     4, 65535, 65535,    60, 65535,    48, 65535,    52, 65535,
    65535, 65535,    13, 65535,    15, 65535, 65535, 65535, 65535,    61, 65535,    63, 65535, 65535,
    65535, 65535, 65535,    14, 65535, 65535, 65535, 65535, 65535, 65535,    62, 65535, 65535, 65535,
    65535, 65535, 65535,    24, 65535, 65535, 65535, 65535, 65535, 65535,    72, 65535, 65535, 65535,
    65535, 65535,    25, 65535,    23, 65535, 65535, 65535, 65535,    73, 65535,    71, 65535, 65535,
    65535,    26, 65535,    18, 65535,    22, 65535, 65535,    74, 65535,    66, 65535,    70, 65535,
       27, 65535,    19, 65535,    17, 65535,    21,    75, 65535,    67, 65535,    65, 65535,    69,
    65535,    28, 65535,    16, 65535,    20, 65535, 65535,    76, 65535,    64, 65535,    68, 65535,
    65535, 65535,    29, 65535,    31, 65535, 65535, 65535, 65535,    77, 65535,    79, 65535, 65535,
    65535, 65535, 65535,    30, 65535, 65535, 65535, 65535, 65535, 65535,    78, 65535, 65535, 65535,
    65535, 65535, 65535,    40, 65535, 65535, 65535, 65535, 65535, 65535,    88, 65535, 65535, 65535,
    65535, 65535,    41, 65535,    39, 65535, 65535, 65535, 65535,    89, 65535,    87, 65535, 65535,
    65535,    42, 65535,    34, 65535,    38, 65535, 65535,    90, 65535,    82, 65535,    86, 65535,
       43, 65535,    35, 65535,    33, 65535,    37,    91, 65535,    83, 65535,    81, 65535,    85,
    65535,    44, 65535,    32, 65535,    36, 65535, 65535,    92, 65535,    80, 65535,    84, 65535,
    65535, 65535,    45, 65535,    47, 65535, 65535, 65535, 65535,    93, 65535,    95, 65535, 65535,
    65535, 65535, 65535,    46, 65535, 65535, 65535, 65535, 65535, 65535,    94, 65535, 65535, 65535
  };

  uint16_t j = XYTable[(y * kMatrixWidth) + x];
  if (j == 65535) {
    return OUT_OF_BOUNDS;
  }
  return panelNum * kLEDS_PER_PANEL + j;
}

uint16_t XY(uint8_t globalX, uint8_t globalY) {
  uint8_t panelCol = globalX / kMatrixWidth;
  uint8_t panelRow = globalY / kMatrixHeight;
  if (panelCol >= kCols || panelRow >= kRows) {
    return OUT_OF_BOUNDS;
  }
  return XYPanel(panelCol, panelRow,
                 globalX % kMatrixWidth,
                 globalY % kMatrixHeight);
}

// ---- control value -> speed and warmth -------------------------------------

void applyControl(uint32_t value) {
  control = value;

  // 0.0 at the fast/cool end, 1.0 at the slow/warm end.
  float t = float(value - MIN_CTRL) / float(MAX_CTRL - MIN_CTRL);

  // Stripe step interval: 8 ms at the fast end, 220 ms at the slow end.
  moveInterval = uint32_t(8 + t * (220 - 8));

  // Colour temperature: cool blue -> warm amber.
  stripeColor = CRGB(uint8_t(  0 + t * (255 -   0)),
                     uint8_t( 90 + t * (130 -  90)),
                     uint8_t(255 + t * ( 15 - 255)));

  // Background follows, much dimmer, so the whole field shifts warmth too.
  bgColor = CRGB(uint8_t( 0 + t * (34 -  0)),
                 uint8_t( 6 + t * (16 -  6)),
                 uint8_t(26 + t * ( 2 - 26)));
}

// ---- UDP -------------------------------------------------------------------

void teensyMAC(uint8_t *out) {
  for (uint8_t i = 0; i < 2; i++) {
    out[i] = (HW_OCOTP_MAC1 >> ((1 - i) * 8)) & 0xFF;
  }
  for (uint8_t i = 0; i < 4; i++) {
    out[i + 2] = (HW_OCOTP_MAC0 >> ((3 - i) * 8)) & 0xFF;
  }
}

void handlePacket() {
  int n = udp.read(packetBuf, sizeof(packetBuf) - 1);
  if (n <= 0) return;
  packetBuf[n] = '\0';

  while (n > 0 && (packetBuf[n - 1] == '\n' || packetBuf[n - 1] == '\r' ||
                   packetBuf[n - 1] == ' ')) {
    packetBuf[--n] = '\0';
  }

  IPAddress from = udp.remoteIP();
  uint16_t fromPort = udp.remotePort();

  if (strcmp(packetBuf, "ping") == 0) {
    snprintf(replyBuf, sizeof(replyBuf), "pong control=%lu uptime=%lu",
             (unsigned long)control, (unsigned long)millis());
  } else if (strcmp(packetBuf, "get") == 0) {
    snprintf(replyBuf, sizeof(replyBuf), "control=%lu step=%lums",
             (unsigned long)control, (unsigned long)moveInterval);
  } else {
    char *end;
    long value = strtol(packetBuf, &end, 10);
    if (end == packetBuf) {
      snprintf(replyBuf, sizeof(replyBuf), "error bad-command '%s'", packetBuf);
    } else {
      uint32_t clamped = (uint32_t)value;
      bool wasClamped = false;
      if (value < (long)MIN_CTRL) { clamped = MIN_CTRL; wasClamped = true; }
      if (value > (long)MAX_CTRL) { clamped = MAX_CTRL; wasClamped = true; }

      applyControl(clamped);

      snprintf(replyBuf, sizeof(replyBuf),
               "ok control=%lu step=%lums rgb=%u,%u,%u%s",
               (unsigned long)control, (unsigned long)moveInterval,
               stripeColor.r, stripeColor.g, stripeColor.b,
               wasClamped ? " (clamped)" : "");
      Serial.print("control -> ");
      Serial.print(control);
      Serial.print("  step ");
      Serial.print(moveInterval);
      Serial.println(" ms");
    }
  }

  udp.beginPacket(from, fromPort);
  udp.write((const uint8_t *)replyBuf, strlen(replyBuf));
  udp.endPacket();
}

// Frame from fan_wall.py: 'F', uint16 LE byte offset into leds[], RGB bytes
// in leds[] order. The chunk that reaches the end of leds[] shows the frame.
// No reply -- ~120 packets/s doesn't need one.
uint32_t lastFrame = 0;
bool streaming = false;

void handleFrame(int size) {
  uint8_t hdr[3];
  udp.read(hdr, 3);
  uint32_t off = hdr[1] | (hdr[2] << 8);
  uint32_t len = size - 3;
  if (off + len > sizeof(leds)) return;   // malformed: drop it
  udp.read((uint8_t *)leds + off, len);
  lastFrame = millis();
  streaming = true;
  if (off + len == sizeof(leds)) FastLED.show();
}

// ---- setup -----------------------------------------------------------------

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);

  Serial.begin(115200);
  if (CrashReport) Serial.print(CrashReport);
  uint32_t wait = millis();
  while (!Serial && millis() - wait < 2000) {
    // not indefinitely — this should run headless too
  }

  teensyMAC(mac);
  Ethernet.begin(mac, teensyIP, dnsIP, gatewayIP, subnetMask);
  Serial.println(Ethernet.localIP());
  udp.begin(LISTEN_PORT);

  Serial.print("Teensy IP: ");
  Serial.println(Ethernet.localIP());
  Serial.print("Link: ");
  Serial.println(Ethernet.linkStatus() == LinkON ? "up" : "down / unknown");
  Serial.print("Listening on UDP ");
  Serial.println(LISTEN_PORT);

  // One output pin per panel. FastLED needs the pin as a compile-time
  // constant, so these can't be a loop. Order matches the FlexIO parallel
  // group for the Teensy 4.1.
  FastLED.addLeds<CHIPSET,  1, COLOR_ORDER>(leds,  0 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET,  0, COLOR_ORDER>(leds,  1 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 24, COLOR_ORDER>(leds,  2 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 25, COLOR_ORDER>(leds,  3 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 19, COLOR_ORDER>(leds,  4 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 18, COLOR_ORDER>(leds,  5 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 14, COLOR_ORDER>(leds,  6 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 15, COLOR_ORDER>(leds,  7 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 17, COLOR_ORDER>(leds,  8 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 16, COLOR_ORDER>(leds,  9 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 22, COLOR_ORDER>(leds, 10 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 23, COLOR_ORDER>(leds, 11 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 20, COLOR_ORDER>(leds, 12 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 21, COLOR_ORDER>(leds, 13 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 26, COLOR_ORDER>(leds, 14 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);
  FastLED.addLeds<CHIPSET, 27, COLOR_ORDER>(leds, 15 * kLEDS_PER_PANEL, kLEDS_PER_PANEL).setCorrection(TypicalSMD5050);

  FastLED.setBrightness(50);

  applyControl(control);
  lastMove = millis();
  lastBeat = millis();
}

// ---- loop ------------------------------------------------------------------

void loop() {
  // Drain everything waiting, not one datagram per pass.
  int size;
  while ((size = udp.parsePacket()) > 0) {
    if (udp.peek() == 'F') handleFrame(size);
    else handlePacket();
  }

  uint32_t now = millis();

  // Stripe is the idle show: it resumes 1 s after frames stop arriving.
  if (streaming && now - lastFrame >= 1000) streaming = false;

  if (!streaming && now - lastMove >= moveInterval) {
    lastMove = now;

    // Fade the whole field toward the current background colour.
    for (uint16_t i = 0; i < NUM_LEDS; i++) {
      nblend(leds[i], bgColor, 30);
    }

    // Advance the stripe, bouncing at both ends.
    yPos += yDir;
    if (yPos >= (int16_t)GRID_H - 1) {
      yPos = GRID_H - 1;
      yDir = -1;
    } else if (yPos <= 0) {
      yPos = 0;
      yDir = 1;
    }

    // Draw one horizontal line at the stripe's position.
    for (uint16_t x = 0; x < GRID_W; x++) {
      uint16_t idx = XY(x, yPos);
      if (idx < NUM_LEDS) {
        leds[idx] += stripeColor;
      }
    }

    FastLED.show();
  }

  // Heartbeat, independent of the animation.
  if (now - lastBeat >= 500) {
    lastBeat = now;
    beatState = !beatState;
    digitalWrite(LED_BUILTIN, beatState ? HIGH : LOW);
  }
}
