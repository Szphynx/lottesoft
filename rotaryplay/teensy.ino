/*
  84-fan wall — UDP video receiver — Teensy 4.1

  Sideways (horizontal) hubs: each 96-LED hub carries 6 fans as
  3 across x 2 down, so one panel is 21 x 14 cells.
  4 x 4 panels = 84 x 56 cells = 12 x 8 fans, less the four
  corner half-panels, giving 84 fans and 1344 live LEDs.

  Wall layout (P = panel number, . = no fans):

        col0   col1   col2   col3
  row0  P0 .   P1     P2     P3 .     <- P0, P3: TOP fan row missing
  row1  P4     P5     P6     P7
  row2  P8     P9     P10    P11
  row3  P12.   P13    P14    P15.     <- P12, P15: BOTTOM fan row missing

  Stack: NativeEthernet (ships with Teensyduino) + FastLED.
  FastLED >= 3.9.8 uses ObjectFLED automatically on Teensy 4.x, so the
  16 addLeds() calls below are driven in parallel, ~3 ms per show().
  Do NOT #define FASTLED_USES_OBJECTFLED — it is implicit.

  Packet format is documented in pi_streamer_spec.md.
*/

#include <NativeEthernet.h>
#include <NativeEthernetUdp.h>
#include <FastLED.h>

// ---- network ---------------------------------------------------------------
IPAddress teensyIP(192, 168, 60, 50);
IPAddress subnetMask(255, 255, 255, 0);
IPAddress gatewayIP(192, 168, 60, 1);   // nothing there; never consulted
IPAddress dnsIP(192, 168, 60, 1);       // same, unused
const uint16_t LISTEN_PORT = 5005;

byte mac[6];
EthernetUDP udp;

// ---- matrix ----------------------------------------------------------------
#define COLOR_ORDER GRB
#define CHIPSET     WS2812B

#define NL 65535   // "no LED at this cell"

const uint8_t kMatrixWidth  = 21;   // one panel: 21 cells wide (3 fans)
const uint8_t kMatrixHeight = 14;   // one panel: 14 cells tall (2 fans)
const uint8_t kCols = 4;            // panels across the wall
const uint8_t kRows = 4;            // panels down the wall
const uint8_t kTotalPanels = kCols * kRows;          // 16

#define kLEDS_PER_PANEL 96
#define NUM_LEDS (kLEDS_PER_PANEL * kTotalPanels)    // 1536 buffered, 1344 lit
#define OUT_OF_BOUNDS (NUM_LEDS)

const uint8_t FRAME_W = kMatrixWidth * kCols;        // 84 cells
const uint8_t FRAME_H = kMatrixHeight * kRows;       // 56 cells

CRGB leds[NUM_LEDS];

// ---- protocol --------------------------------------------------------------
const uint8_t  SYNC_BYTE   = 0xAA;
const uint16_t ROW_BYTES   = FRAME_W * 3;            // 252
const uint16_t PACKET_SIZE = 3 + ROW_BYTES;          // 255

// Render anyway if a frame is still incomplete after this long, so one
// lost packet costs a slightly torn frame instead of stalling output.
const uint32_t FRAME_TIMEOUT_MS = 120;

uint8_t  frameBuf[FRAME_H][ROW_BYTES];
bool     rowReceived[FRAME_H];
uint8_t  rowsPending = FRAME_H;
uint8_t  currentSeq  = 0;
bool     seqValid    = false;
uint32_t frameStarted = 0;

uint8_t packetBuf[PACKET_SIZE];

// ---- stats (printed once a second on Serial) -------------------------------
uint32_t framesShown = 0, framesTorn = 0, lastStat = 0;

// ---- panel mapping ---------------------------------------------------------
// One sideways panel. Top fan row: fans at LEDs 0-47.
// Bottom fan row: fans at LEDs 48-95.
const uint16_t XYTable[] = {
    NL, NL, NL,  8, NL, NL, NL, NL, NL, NL, 24, NL, NL, NL, NL, NL, NL, 40, NL, NL, NL,
    NL, NL,  9, NL,  7, NL, NL, NL, NL, 25, NL, 23, NL, NL, NL, NL, 41, NL, 39, NL, NL,
    NL, 10, NL,  2, NL,  6, NL, NL, 26, NL, 18, NL, 22, NL, NL, 42, NL, 34, NL, 38, NL,
    11, NL,  3, NL,  1, NL,  5, 27, NL, 19, NL, 17, NL, 21, 43, NL, 35, NL, 33, NL, 37,
    NL, 12, NL,  0, NL,  4, NL, NL, 28, NL, 16, NL, 20, NL, NL, 44, NL, 32, NL, 36, NL,
    NL, NL, 13, NL, 15, NL, NL, NL, NL, 29, NL, 31, NL, NL, NL, NL, 45, NL, 47, NL, NL,
    NL, NL, NL, 14, NL, NL, NL, NL, NL, NL, 30, NL, NL, NL, NL, NL, NL, 46, NL, NL, NL,
    NL, NL, NL, 56, NL, NL, NL, NL, NL, NL, 72, NL, NL, NL, NL, NL, NL, 88, NL, NL, NL,
    NL, NL, 57, NL, 55, NL, NL, NL, NL, 73, NL, 71, NL, NL, NL, NL, 89, NL, 87, NL, NL,
    NL, 58, NL, 50, NL, 54, NL, NL, 74, NL, 66, NL, 70, NL, NL, 90, NL, 82, NL, 86, NL,
    59, NL, 51, NL, 49, NL, 53, 75, NL, 67, NL, 65, NL, 69, 91, NL, 83, NL, 81, NL, 85,
    NL, 60, NL, 48, NL, 52, NL, NL, 76, NL, 64, NL, 68, NL, NL, 92, NL, 80, NL, 84, NL,
    NL, NL, 61, NL, 63, NL, NL, NL, NL, 77, NL, 79, NL, NL, NL, NL, 93, NL, 95, NL, NL,
    NL, NL, NL, 62, NL, NL, NL, NL, NL, NL, 78, NL, NL, NL, NL, NL, NL, 94, NL, NL, NL,
};

uint16_t XYPanel(uint8_t panelCol, uint8_t panelRow, uint8_t x, uint8_t y) {
  if ((x >= kMatrixWidth) || (y >= kMatrixHeight)) {
    return OUT_OF_BOUNDS;
  }

  uint16_t panelNum = panelRow * kCols + panelCol;

  uint16_t j = XYTable[(y * kMatrixWidth) + x];
  if (j == NL) {
    return OUT_OF_BOUNDS;
  }

  // ---- Corner exemptions --------------------------------------
  // Top-left (0) and top-right (3): top fan row has no fans.
  // The bottom-row fans are plugged into ports 1-3, so their LEDs
  // arrive first on the cable: shift them forward by 48.
  if (panelNum == 0 || panelNum == 3) {
    if (j < 48) {
      return OUT_OF_BOUNDS;   // empty top row
    }
    j = j - 48;               // 48-95 becomes 0-47
  }

  // Bottom-left (12) and bottom-right (15): bottom fan row has no fans.
  // The top-row fans are already in ports 1-3, nothing moves.
  if (panelNum == 12 || panelNum == 15) {
    if (j >= 48) {
      return OUT_OF_BOUNDS;   // empty bottom row
    }
  }
  // --------------------------------------------------------------

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

// ---- frame handling --------------------------------------------------------

void resetFrameTracking(uint8_t seq) {
  for (uint8_t r = 0; r < FRAME_H; r++) rowReceived[r] = false;
  rowsPending  = FRAME_H;
  currentSeq   = seq;
  seqValid     = true;
  frameStarted = millis();
}

void renderFrameToLeds() {
  for (uint8_t y = 0; y < FRAME_H; y++) {
    const uint8_t *src = frameBuf[y];
    for (uint8_t x = 0; x < FRAME_W; x++) {
      uint16_t idx = XY(x, y);
      if (idx < NUM_LEDS) {
        uint16_t o = x * 3;
        leds[idx] = CRGB(src[o], src[o + 1], src[o + 2]);
      }
    }
  }
  FastLED.show();
  framesShown++;
}

// ---- MAC from fuses --------------------------------------------------------

void teensyMAC(uint8_t *out) {
  for (uint8_t i = 0; i < 2; i++) {
    out[i] = (HW_OCOTP_MAC1 >> ((1 - i) * 8)) & 0xFF;
  }
  for (uint8_t i = 0; i < 4; i++) {
    out[i + 2] = (HW_OCOTP_MAC0 >> ((3 - i) * 8)) & 0xFF;
  }
}

// ---- setup -----------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  if (CrashReport) Serial.print(CrashReport);
  uint32_t wait = millis();
  while (!Serial && millis() - wait < 2000) { }

  teensyMAC(mac);
  Ethernet.begin(mac, teensyIP, dnsIP, gatewayIP, subnetMask);
  udp.begin(LISTEN_PORT);

  Serial.print("Teensy IP: ");   Serial.println(Ethernet.localIP());
  Serial.print("Link: ");
  Serial.println(Ethernet.linkStatus() == LinkON ? "up" : "down / unknown");
  Serial.print("Frame: ");       Serial.print(FRAME_W);
  Serial.print(" x ");           Serial.println(FRAME_H);
  Serial.print("Packet size: "); Serial.println(PACKET_SIZE);
  Serial.print("Listening on UDP "); Serial.println(LISTEN_PORT);

  // One output pin per panel, in panel-number order: panel 0 -> pin 1,
  // panel 1 -> pin 0, and so on. Pin order is the FlexIO parallel group.
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

  FastLED.setBrightness(170);

  // Hard ceiling on total draw. FastLED scales a frame down only if that
  // frame would exceed the budget; it never blanks. 45 A of the PSU's 60 A.
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 45000);

  FastLED.clear(true);
  resetFrameTracking(0);
  seqValid = false;
  lastStat = millis();
}

// ---- loop ------------------------------------------------------------------

void loop() {
  // Drain every waiting datagram, not one per pass.
  int size;
  while ((size = udp.parsePacket()) > 0) {
    if (size != PACKET_SIZE) {
      udp.read(packetBuf, sizeof(packetBuf));   // discard malformed
      continue;
    }
    udp.read(packetBuf, PACKET_SIZE);
    if (packetBuf[0] != SYNC_BYTE) continue;

    uint8_t seq = packetBuf[1];
    uint8_t row = packetBuf[2];
    if (row >= FRAME_H) continue;

    // A new sequence number means the previous frame is over. Show what
    // arrived of it rather than waiting for rows that will never come.
    if (!seqValid || seq != currentSeq) {
      if (seqValid && rowsPending < FRAME_H) {
        renderFrameToLeds();
        framesTorn++;
      }
      resetFrameTracking(seq);
    }

    memcpy(frameBuf[row], &packetBuf[3], ROW_BYTES);
    if (!rowReceived[row]) {
      rowReceived[row] = true;
      rowsPending--;
    }

    if (rowsPending == 0) {
      renderFrameToLeds();
      seqValid = false;        // wait for the next sequence number
    }
  }

  // Safety net: never let a partial frame sit on screen indefinitely.
  if (seqValid && rowsPending > 0 && rowsPending < FRAME_H &&
      millis() - frameStarted > FRAME_TIMEOUT_MS) {
    renderFrameToLeds();
    framesTorn++;
    seqValid = false;
  }

  uint32_t now = millis();
  if (now - lastStat >= 1000) {
    lastStat = now;
    Serial.print("fps ");   Serial.print(framesShown);
    Serial.print("  torn "); Serial.println(framesTorn);
    framesShown = 0;
    framesTorn  = 0;
  }
}
