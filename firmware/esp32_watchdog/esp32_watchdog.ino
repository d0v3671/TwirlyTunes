
/*
  TWIRLYTUNES - FINAL ESP32 MOTOR CONTROLLER

  Hardware:
    ESP32 GPIO26 -> L298N IN1
    ESP32 GPIO27 -> L298N IN2
    ESP32 GPIO25 -> L298N ENA
    ESP32 GND    -> L298N GND

  Communication:
    Raspberry Pi -> ESP32 via USB serial
    Baud rate: 115200

  Commands:
    CRY  -> Start motor / keep motor running
    STOP -> Stop motor immediately

  Motor behavior:
    Start PWM = 80
    Run PWM   = 55
    Ramp time = 1500 ms

  Safety:
    Motor OFF at startup
    Motor OFF if serial commands stop arriving
    Motor OFF on invalid commands

  Arduino-ESP32 core 3.x
*/

#include <Arduino.h>

// ---------------- PIN CONFIGURATION ----------------

constexpr int IN1 = 26;
constexpr int IN2 = 27;
constexpr int ENA = 25;

// ---------------- MOTOR SETTINGS ----------------

constexpr int START_PWM = 60;
constexpr int RUN_PWM = 48;

constexpr unsigned long RAMP_DURATION_MS = 1500;

// Raspberry Pi sends CRY approximately every 500 ms.
// Stop motor if commands stop arriving.
constexpr unsigned long WATCHDOG_MS = 1500;

// ---------------- PWM SETTINGS ----------------

constexpr int PWM_FREQUENCY = 1200;
constexpr int PWM_RESOLUTION = 8;

// ---------------- MOTOR STATE ----------------

bool motorRunning = false;

unsigned long motorStartTime = 0;
unsigned long lastCryCommand = 0;

// ---------------- STOP MOTOR ----------------

void stopMotor() {
  ledcWrite(ENA, 0);

  digitalWrite(IN1, LOW);
  digitalWrite(IN2, LOW);

  motorRunning = false;

  Serial.println("MOTOR STOPPED");
}

// ---------------- START MOTOR ----------------

void startMotor() {
  digitalWrite(IN1, HIGH);
  digitalWrite(IN2, LOW);

  motorStartTime = millis();
  lastCryCommand = motorStartTime;

  motorRunning = true;

  ledcWrite(ENA, START_PWM);

  Serial.println("MOTOR STARTED");
}

// ---------------- UPDATE MOTOR SPEED ----------------

void updateMotorSpeed() {
  if (!motorRunning) {
    return;
  }

  unsigned long now = millis();

  // Safety timeout
  if (now - lastCryCommand >= WATCHDOG_MS) {
    stopMotor();
    Serial.println("WATCHDOG TIMEOUT");
    return;
  }

  unsigned long elapsed = now - motorStartTime;

  int pwm;

  if (elapsed >= RAMP_DURATION_MS) {
    pwm = RUN_PWM;
  }
  else {
    pwm = START_PWM -
      ((START_PWM - RUN_PWM) * elapsed) /
      RAMP_DURATION_MS;
  }

  ledcWrite(ENA, pwm);
}

// ---------------- PROCESS SERIAL COMMAND ----------------

void processCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command == "CRY") {

    lastCryCommand = millis();

    // Do not restart ramp for repeated CRY commands
    if (!motorRunning) {
      startMotor();
    }

    Serial.println("ACK CRY");
  }
  else if (command == "STOP") {
    stopMotor();
    Serial.println("ACK STOP");
  }
  else {
    stopMotor();
    Serial.println("INVALID COMMAND");
  }
}

// ---------------- SETUP ----------------

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);

  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);

  digitalWrite(IN1, LOW);
  digitalWrite(IN2, LOW);

  // Arduino-ESP32 core 3.x
  if (!ledcAttach(ENA, PWM_FREQUENCY, PWM_RESOLUTION)) {
    Serial.println("ERROR: PWM SETUP FAILED");

    while (true) {
      digitalWrite(IN1, LOW);
      digitalWrite(IN2, LOW);
      delay(100);
    }
  }

  stopMotor();

  Serial.println("------------------------------");
  Serial.println("TWIRLYTUNES MOTOR CONTROLLER");
  Serial.println("START PWM: 80");
  Serial.println("RUN PWM: 55");
  Serial.println("RAMP: 1500 ms");
  Serial.println("WATCHDOG: 1500 ms");
  Serial.println("READY - CRY / STOP");
  Serial.println("------------------------------");
}

// ---------------- MAIN LOOP ----------------

void loop() {

  // Receive Raspberry Pi commands
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');

    processCommand(command);
  }

  // Update motor speed without blocking
  updateMotorSpeed();
}
