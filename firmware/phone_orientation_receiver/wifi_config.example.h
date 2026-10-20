#pragma once

// Copy to wifi_config.h and edit locally; wifi_config.h is ignored by Git.
// 0: join your router; 1: create an ESP32 access point.
#define USE_AP 0

const char* AP_SSID = "YOUR_ESP32_AP_NAME";
const char* AP_PASS = "change-me-123";  // At least 8 characters.

const char* STA_SSID = "YOUR_WIFI_NAME";
const char* STA_PASS = "YOUR_WIFI_PASSWORD";
