#pragma once

#include <cstddef>
#include <cstring>

namespace orientation_commands {

constexpr std::size_t kIdLength = 32;
constexpr std::size_t kQueueCapacity = 8;
constexpr std::size_t kRecentCapacity = 32;

enum class Command { MoveBack, Rotate, MoveSide };
enum class SubmitResult { Accepted, Duplicate, Invalid, Conflict, QueueFull };

inline const char* commandName(Command command) {
  switch (command) {
    case Command::MoveBack: return "MOVE_BACK";
    case Command::Rotate: return "ROTATE";
    case Command::MoveSide: return "MOVE_SIDE";
  }
  return "";
}

inline bool parseCommand(const char* text, Command& command) {
  if (!text) return false;
  if (std::strcmp(text, "MOVE_BACK") == 0) command = Command::MoveBack;
  else if (std::strcmp(text, "ROTATE") == 0) command = Command::Rotate;
  else if (std::strcmp(text, "MOVE_SIDE") == 0) command = Command::MoveSide;
  else return false;
  return true;
}

inline bool validId(const char* id) {
  if (!id || std::strlen(id) != kIdLength) return false;
  for (std::size_t i = 0; i < kIdLength; ++i) {
    if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) {
      return false;
    }
  }
  return true;
}

struct Signal {
  char id[kIdLength + 1];
  Command command;
};

// Fixed storage only. The caller must guard submit/pop when sharing across tasks.
class SignalQueue {
 public:
  SubmitResult submit(const char* id, const char* text) {
    Command command;
    if (!validId(id) || !parseCommand(text, command)) return SubmitResult::Invalid;

    for (std::size_t i = 0; i < recentCount_; ++i) {
      if (std::strcmp(recent_[i].id, id) == 0) {
        return recent_[i].command == command ? SubmitResult::Duplicate
                                            : SubmitResult::Conflict;
      }
    }
    if (queueCount_ == kQueueCapacity) return SubmitResult::QueueFull;

    Signal signal;
    std::memcpy(signal.id, id, kIdLength + 1);
    signal.command = command;
    queue_[(queueHead_ + queueCount_) % kQueueCapacity] = signal;
    ++queueCount_;

    // Record only after enqueue succeeds, so a full-queue rejection is retryable.
    recent_[recentNext_] = signal;
    recentNext_ = (recentNext_ + 1) % kRecentCapacity;
    if (recentCount_ < kRecentCapacity) ++recentCount_;
    return SubmitResult::Accepted;
  }

  bool pop(Signal& signal) {
    if (queueCount_ == 0) return false;
    signal = queue_[queueHead_];
    queueHead_ = (queueHead_ + 1) % kQueueCapacity;
    --queueCount_;
    return true;
  }

 private:
  Signal queue_[kQueueCapacity] = {};
  Signal recent_[kRecentCapacity] = {};
  std::size_t queueHead_ = 0;
  std::size_t queueCount_ = 0;
  std::size_t recentNext_ = 0;
  std::size_t recentCount_ = 0;
};

}  // namespace orientation_commands
