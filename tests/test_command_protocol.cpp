#include "../firmware/phone_orientation_receiver/command_protocol.h"

#include <cassert>
#include <cstdio>
#include <cstring>

using namespace orientation_commands;

static void makeId(unsigned number, char (&id)[33]) {
  std::snprintf(id, sizeof(id), "%032x", number);
}

int main() {
  const char* id = "0123456789abcdef0123456789abcdef";
  Signal signal;

  // Only the exact symbolic allowlist and lowercase hexadecimal IDs are accepted.
  SignalQueue validation;
  const char* badIds[] = {nullptr, "", "0123456789abcdef0123456789abcde",
                         "0123456789abcdef0123456789abcdef0",
                         "0123456789ABCDEF0123456789abcdef",
                         "0123456789abcdef0123456789abcdeg",
                         "01234567-89ab-cdef-0123-456789abcdef"};
  for (const char* bad : badIds) {
    assert(validation.submit(bad, "MOVE_BACK") == SubmitResult::Invalid);
  }
  const char* badCommands[] = {nullptr, "", "move_back", "MOVE_BACK ",
                              "MOVE_SIDEWAYS", "BACK", "SIGTERM"};
  for (const char* bad : badCommands) {
    assert(validation.submit(id, bad) == SubmitResult::Invalid);
  }
  assert(!validation.pop(signal));
  const char* commands[] = {"MOVE_BACK", "ROTATE", "MOVE_SIDE"};
  for (unsigned i = 0; i < 3; ++i) {
    char nextId[33];
    makeId(i, nextId);
    assert(validation.submit(nextId, commands[i]) == SubmitResult::Accepted);
    assert(validation.pop(signal));
    assert(std::strcmp(signal.id, nextId) == 0);
    assert(std::strcmp(commandName(signal.command), commands[i]) == 0);
  }

  // Exact retries acknowledge success without adding a second signal.
  SignalQueue duplicates;
  assert(duplicates.submit(id, "MOVE_BACK") == SubmitResult::Accepted);
  assert(duplicates.submit(id, "MOVE_BACK") == SubmitResult::Duplicate);
  assert(duplicates.submit(id, "ROTATE") == SubmitResult::Conflict);
  assert(duplicates.pop(signal));
  assert(!duplicates.pop(signal));
  assert(duplicates.submit(id, "MOVE_BACK") == SubmitResult::Duplicate);
  assert(duplicates.submit(id, "MOVE_SIDE") == SubmitResult::Conflict);
  assert(!duplicates.pop(signal));

  // Full queues preserve FIFO and do not remember rejected IDs as accepted.
  SignalQueue fifo;
  for (unsigned i = 0; i < kQueueCapacity; ++i) {
    char nextId[33];
    makeId(i, nextId);
    assert(fifo.submit(nextId, "ROTATE") == SubmitResult::Accepted);
  }
  char overflowId[33], firstId[33];
  makeId(kQueueCapacity, overflowId);
  makeId(0, firstId);
  assert(fifo.submit(overflowId, "MOVE_SIDE") == SubmitResult::QueueFull);
  assert(fifo.submit(firstId, "ROTATE") == SubmitResult::Duplicate);
  assert(fifo.submit(firstId, "MOVE_BACK") == SubmitResult::Conflict);
  assert(fifo.pop(signal));
  assert(std::strcmp(signal.id, firstId) == 0);
  assert(fifo.submit(overflowId, "MOVE_SIDE") == SubmitResult::Accepted);
  for (unsigned i = 1; i <= kQueueCapacity; ++i) {
    char expectedId[33];
    makeId(i, expectedId);
    assert(fifo.pop(signal));
    assert(std::strcmp(signal.id, expectedId) == 0);
    assert(signal.command == (i == kQueueCapacity ? Command::MoveSide : Command::Rotate));
  }
  assert(!fifo.pop(signal));

  // Only the latest 32 accepted IDs are retained; reboot creates an empty queue.
  SignalQueue bounded;
  for (unsigned i = 0; i <= kRecentCapacity; ++i) {
    char nextId[33];
    makeId(i, nextId);
    assert(bounded.submit(nextId, "MOVE_BACK") == SubmitResult::Accepted);
    assert(bounded.pop(signal));
  }
  char retainedId[33], evictedId[33];
  makeId(1, retainedId);
  makeId(0, evictedId);
  assert(bounded.submit(retainedId, "MOVE_BACK") == SubmitResult::Duplicate);
  assert(bounded.submit(evictedId, "ROTATE") == SubmitResult::Accepted);
  SignalQueue rebooted;
  assert(rebooted.submit(retainedId, "ROTATE") == SubmitResult::Accepted);

  std::puts("PASS: command validation, idempotency, conflicts, FIFO/full retry, bounded history, reboot state");
}
