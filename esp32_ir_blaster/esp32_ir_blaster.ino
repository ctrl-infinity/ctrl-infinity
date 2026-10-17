// esp32_ir_blaster.ino: a LAN-only HTTP IR blaster for the Airtel DTH box.
//
// Library: IRremoteESP8266 (Library Manager). Board: esp32 by Espressif, core 2.0.17.
//   Core 3.x broke IRremoteESP8266's receive timer (sending still works), so pin 2.0.17
//   if you want to learn codes with this board.
//
// Wiring, either way:
//   (a) best: 940 nm IR LED glued 1-2 cm in front of the STB's IR window on a 1-2 m wire.
//       GPIO4 -> 100 ohm -> LED anode, LED cathode -> GND. Can't miss, can't hit the TV.
//   (b) room-wide: GPIO4 -> 1k -> 2N2222 base; collector -> LED cathode;
//       LED anode -> 33 ohm -> 5V; emitter -> GND.
//   Receiver (learning only): TSOP38238 / VS1838B OUT -> GPIO14, VCC -> 3V3, GND -> GND.
//
// API:
//   GET  /health                                      -> {"ok":true,...}
//   GET  /ir?proto=NEC&code=0x20DF10EF&bits=32&repeat=0
//   POST /raw?hz=38000      body: "9000,4500,560,560,..." (microseconds, mark first)
//   GET  /learn[?clear=1]   -> last code seen by the receiver, {} if none

#include <WiFi.h>
#include <WebServer.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRrecv.h>
#include <IRutils.h>

const char* WIFI_SSID = "YOUR_SSID";
const char* WIFI_PASS = "YOUR_PASSWORD";
// Fixed IP (also reserve it in the router) so the launcher never loses the blaster.
IPAddress LOCAL_IP(192, 168, 1, 41), GATEWAY(192, 168, 1, 1), SUBNET(255, 255, 255, 0);

const uint16_t IR_LED_PIN = 4;
const uint16_t IR_RECV_PIN = 14;
const uint16_t RAW_MAX = 600;

IRsend irsend(IR_LED_PIN);
IRrecv irrecv(IR_RECV_PIN, 1024, 50, true);
decode_results results;
WebServer server(80);
String lastLearned = "{}";

void sendJson(int code, const String& body) { server.send(code, "application/json", body); }

void handleHealth() {
  sendJson(200, String("{\"ok\":true,\"uptime_s\":") + (millis() / 1000) + ",\"rssi\":" + WiFi.RSSI() + "}");
}

void handleIr() {
  if (!server.hasArg("proto") || !server.hasArg("code")) { sendJson(400, "{\"error\":\"need proto and code\"}"); return; }
  decode_type_t proto = strToDecodeType(server.arg("proto").c_str());
  if (proto == decode_type_t::UNKNOWN) { sendJson(400, "{\"error\":\"unknown protocol\"}"); return; }
  uint64_t code = strtoull(server.arg("code").c_str(), nullptr, 0);  // accepts 0x...
  uint16_t bits = server.hasArg("bits") ? server.arg("bits").toInt() : IRsend::defaultBits(proto);
  uint16_t repeat = server.hasArg("repeat") ? server.arg("repeat").toInt() : 0;
  irrecv.disableIRIn();  // don't "learn" our own transmission
  bool ok = irsend.send(proto, code, bits, repeat);
  irrecv.enableIRIn();
  sendJson(ok ? 200 : 500, ok ? "{\"ok\":true}" : "{\"ok\":false}");
}

void handleRaw() {
  static uint16_t buf[RAW_MAX];
  uint16_t hz = server.hasArg("hz") ? server.arg("hz").toInt() : 38000;
  String body = server.arg("plain");
  uint16_t n = 0;
  int start = 0;
  while (start < (int)body.length() && n < RAW_MAX) {
    int comma = body.indexOf(',', start);
    if (comma < 0) comma = body.length();
    buf[n++] = body.substring(start, comma).toInt();
    start = comma + 1;
  }
  if (n == 0) { sendJson(400, "{\"error\":\"empty body\"}"); return; }
  irrecv.disableIRIn();
  irsend.sendRaw(buf, n, hz / 1000);
  irrecv.enableIRIn();
  sendJson(200, String("{\"ok\":true,\"pulses\":") + n + "}");
}

void handleLearn() {
  String out = lastLearned;
  if (server.hasArg("clear")) lastLearned = "{}";
  sendJson(200, out);
}

void captureIr() {
  if (!irrecv.decode(&results)) return;
  if (!results.repeat && !results.overflow) {  // skip NEC "repeat" frames and garbage
    String j = "{\"proto\":\"" + typeToString(results.decode_type) + "\"";
    j += ",\"code\":\"0x" + uint64ToString(results.value, 16) + "\"";
    j += ",\"bits\":" + String(results.bits);
    if (results.decode_type == decode_type_t::UNKNOWN) {
      uint16_t* raw = resultToRawArray(&results);
      uint16_t len = getCorrectedRawLength(&results);
      j += ",\"raw\":[";
      for (uint16_t i = 0; i < len; i++) { if (i) j += ","; j += raw[i]; }
      j += "]";
      delete[] raw;
    }
    j += "}";
    lastLearned = j;
    Serial.println(j);
  }
  irrecv.resume();
}

void setup() {
  Serial.begin(115200);
  irsend.begin();
  irrecv.enableIRIn();
  WiFi.mode(WIFI_STA);
  WiFi.config(LOCAL_IP, GATEWAY, SUBNET);
  WiFi.setSleep(false);  // modem sleep adds 100+ ms random latency: it would wreck inter-digit timing
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(250); Serial.print('.'); }
  Serial.printf("\nIR blaster at http://%s\n", WiFi.localIP().toString().c_str());
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/ir", HTTP_GET, handleIr);
  server.on("/raw", HTTP_POST, handleRaw);
  server.on("/learn", HTTP_GET, handleLearn);
  server.begin();
}

void loop() {
  server.handleClient();
  captureIr();
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(500); }
}
