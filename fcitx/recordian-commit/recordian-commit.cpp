/*
 * Fcitx5 module: streaming composition + one-shot commit for Recordian.
 *
 * DBus API (session bus, service org.fcitx.Fcitx5, path /recordian,
 * interface org.fcitx.Fcitx.Recordian1):
 *
 *   Ping() -> s
 *       Liveness probe, returns "ok".
 *
 *   BeginSession(s initial_preedit) -> s
 *       Bind a streaming session to the *currently focused* input context.
 *       Refuses dummy frontends, contexts without focus, and password /
 *       sensitive contexts. Also refuses when the context already shows a
 *       non-empty preedit that no live Recordian session owns (e.g. the
 *       user is composing with Rime/pinyin): that composition belongs to
 *       the user and must never be overwritten. Returns a descriptor:
 *         "<token> preedit=<0|1> frontend=<name> program=<name>"
 *       preedit=1 means the client declared the Preedit capability, so
 *       UpdatePreedit will render inline; with preedit=0 streaming is
 *       preview-only on the Python side and only CommitSession writes
 *       (preview-only UpdatePreedit calls still refresh the TTL clock).
 *       The descriptor also carries "segments=1": this bridge accepts
 *       CommitSegment on the same token. Older bridges omit that marker.
 *       Starting a new session on the same context supersedes and ERASES
 *       the previous entry (session capacity counts live sessions only)
 *       and replaces its preedit, leaving no residue.
 *
 *   CommitSegment(s token, u sequence, s text) -> s
 *       Commit one continuous chunk on the session bound at Begin time.
 *       sequence starts at 1 and must advance by exactly one. A duplicate
 *       or skipped sequence is rejected and writes nothing; the token and
 *       the expected sequence stay as they were. A successful segment
 *       keeps the SAME token active (focus, typing, reset, sensitivity,
 *       TTL, and foreign-preedit guards apply on every call) and refreshes
 *       the inactivity clock. Returns "segment <n> <frontend> <program>"
 *       or "segment <n> cleared" for an empty chunk. Final CommitSession
 *       is still what consumes the token. When the context has no
 *       surrounding snapshot yet, the echo is accepted only if this call's
 *       own CommitString was seen and the new snapshot is exactly that
 *       committed string with the caret at its Unicode end. A longer
 *       buffer, a different string, or a caret that is not at the end
 *       still invalidates.
 *
 *   UpdatePreedit(s token, s text) -> s
 *       Replace the preedit text of the bound context. Never commits,
 *       never steals focus. Fails with StaleSession once the session was
 *       invalidated (focus lost, context destroyed, user typed or
 *       navigated — a bare modifier press such as Control_R does not,
 *       capability became sensitive, superseded by a newer session). If
 *       the bound context cannot be resolved it fails closed: the session
 *       is invalidated instead of touching an unknown context.
 *
 *   CommitSession(s token, s text) -> s
 *       Clear the preedit and commit the final text exactly once. The
 *       session is invalidated first and a second CommitSession with the
 *       same token fails with StaleSession. Fails instead of committing
 *       when the bound context lost focus or was destroyed — Recordian
 *       must never re-focus a window to force the commit through.
 *
 *   CancelSession(s token) -> s
 *       Clear the preedit this addon wrote (a preedit the user's IME has
 *       since replaced is left untouched) and invalidate the session
 *       without committing. Returns "already_gone" for unknown tokens.
 *
 *   CommitText(s text) -> s
 *       Legacy one-shot commit for non-streaming callers. Strictly requires
 *       a focused, non-dummy, non-sensitive context (no fallback to
 *       mostRecentInputContext).
 *
 * This does not go through Rime, and it does not update the Rime user
 * dictionary.
 *
 * NOTE on preedit guarantees: some toolkits commit the client preedit by
 * themselves — native GTK runs observed auto-commit on focus-out and on
 * click, and set_text can land without an IM Reset. Inline preedit is
 * therefore a *preview*, and CommitSession is NOT the only operation that
 * can result in committed text. What this addon does bound itself to: no
 * duplicate writes and no wrong-context writes by the addon — every write
 * goes through the session bound at Begin time (or a strictly focused
 * CommitText), superseded/stale/foreign sessions are refused rather than
 * rolled back everywhere. Universal preedit rollback is a known platform
 * limitation (Recordian-22t).
 *
 * ERROR PROTOCOL: every MethodCallError message embeds its full DBus error
 * name in parentheses ("... (org.fcitx.Fcitx.Recordian.Error.StaleSession)").
 * busctl strips the structured error name from stderr ("Call failed: <msg>"
 * only), so the embedded token is the machine-readable contract the Python
 * side parses; clients must still fail closed on messages without a token.
 *
 * TTL: sessions expire after 120 s of INACTIVITY (last successful preedit
 * update), not 120 s after Begin — long dictations stay alive as long as
 * they keep updating.
 */
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <limits>
#include <mutex>
#include <random>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include <fcitx-utils/utf8.h>

#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/dbus/objectvtable.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputcontextmanager.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>

#include "dbus_public.h"

namespace {

constexpr char kPath[] = "/recordian";
constexpr char kInterface[] = "org.fcitx.Fcitx.Recordian1";
constexpr char kError[] = "org.fcitx.Fcitx.Recordian.Error.NoInputContext";
constexpr char kErrorStale[] = "org.fcitx.Fcitx.Recordian.Error.StaleSession";
constexpr char kErrorBusy[] = "org.fcitx.Fcitx.Recordian.Error.SessionBusy";
constexpr char kErrorPreedit[] =
    "org.fcitx.Fcitx.Recordian.Error.ExistingPreedit";
constexpr char kErrorSequence[] =
    "org.fcitx.Fcitx.Recordian.Error.BadSequence";

constexpr auto kSessionTTL = std::chrono::seconds(120);
constexpr std::size_t kMaxSessions = 8;

// Snapshot of SurroundingText. cursor/anchor are Unicode scalar offsets
// (fcitx SurroundingText), while text is UTF-8 bytes.
struct SurroundSnap {
    bool valid = false;
    std::string text;
    unsigned cursor = 0;
    unsigned anchor = 0;
};

bool sameSnap(const SurroundSnap &a, const SurroundSnap &b) {
    return a.valid && b.valid && a.text == b.text && a.cursor == b.cursor &&
           a.anchor == b.anchor;
}

// Caret collapsed at the last Unicode scalar. Rejects a mid-string caret
// and a non-empty selection.
bool caretAtUtf8End(const SurroundSnap &snap) {
    if (!snap.valid) {
        return false;
    }
    const auto chars = fcitx::utf8::lengthValidated(snap.text);
    if (chars == fcitx::utf8::INVALID_LENGTH) {
        return false;
    }
    return snap.cursor == static_cast<unsigned>(chars) && snap.anchor == snap.cursor;
}

// Byte offset of the first byte of Unicode character `chars`, or npos.
size_t utf8ByteOffset(const std::string &text, unsigned chars) {
    if (chars == 0) {
        return 0;
    }
    if (text.empty()) {
        return static_cast<size_t>(-1);
    }
    const auto n = fcitx::utf8::lengthValidated(text);
    if (n == fcitx::utf8::INVALID_LENGTH ||
        static_cast<size_t>(chars) > n) {
        return static_cast<size_t>(-1);
    }
    return static_cast<size_t>(
        std::distance(text.begin(), fcitx::utf8::nextNChar(text.begin(), chars)));
}

// Replace the selected Unicode range with `inserted`. Collapses the caret
// to the end of the insertion. Returns false when indexes are not valid
// Unicode offsets into `before`.
bool predictCommittedSurround(const SurroundSnap &before,
                              const std::string &inserted, SurroundSnap *after) {
    if (after == nullptr || !before.valid) {
        return false;
    }
    const auto insertedChars = fcitx::utf8::lengthValidated(inserted);
    const auto beforeChars = fcitx::utf8::lengthValidated(before.text);
    if (insertedChars == fcitx::utf8::INVALID_LENGTH ||
        beforeChars == fcitx::utf8::INVALID_LENGTH) {
        return false;
    }
    if (static_cast<size_t>(before.cursor) > beforeChars ||
        static_cast<size_t>(before.anchor) > beforeChars) {
        return false;
    }
    const unsigned lo = std::min(before.cursor, before.anchor);
    const unsigned hi = std::max(before.cursor, before.anchor);
    const size_t loByte = utf8ByteOffset(before.text, lo);
    const size_t hiByte = utf8ByteOffset(before.text, hi);
    if (loByte == static_cast<size_t>(-1) || hiByte == static_cast<size_t>(-1) ||
        loByte > hiByte || hiByte > before.text.size()) {
        return false;
    }
    const auto insertedCount = static_cast<unsigned>(insertedChars);
    if (static_cast<size_t>(lo) + insertedChars > std::numeric_limits<unsigned>::max()) {
        return false;
    }
    after->valid = true;
    after->text = before.text.substr(0, loByte) + inserted + before.text.substr(hiByte);
    after->cursor = lo + insertedCount;
    after->anchor = after->cursor;
    return true;
}

SurroundSnap readSurround(const fcitx::InputContext *ic) {
    SurroundSnap snap;
    if (ic == nullptr) {
        return snap;
    }
    const auto &surround = ic->surroundingText();
    if (!surround.isValid()) {
        return snap;
    }
    const auto chars = fcitx::utf8::lengthValidated(surround.text());
    if (chars == fcitx::utf8::INVALID_LENGTH) {
        return snap;
    }
    if (static_cast<size_t>(surround.cursor()) > chars ||
        static_cast<size_t>(surround.anchor()) > chars) {
        return snap;
    }
    snap.valid = true;
    snap.text = surround.text();
    snap.cursor = surround.cursor();
    snap.anchor = surround.anchor();
    return snap;
}

// One outstanding CommitSegment whose client echo we may accept.
// Cleared on ack, cancel, failure, and TTL. A later event may repeat the
// already-accepted snapshot; any other text or caret invalidates.
struct SegmentAck {
    bool armed = false;
    bool poisoned = false;
    bool commitSeen = false;
    uint32_t sequence = 0;
    fcitx::ICUUID uuid{};
    std::string requested;
    SurroundSnap before{};
    SurroundSnap after{};
    bool afterKnown = false;
    bool hasAccepted = false;
    SurroundSnap accepted{};
};

void clearPendingAck(SegmentAck *ack) {
    if (ack == nullptr) {
        return;
    }
    const bool hasAccepted = ack->hasAccepted;
    const SurroundSnap accepted = ack->accepted;
    *ack = SegmentAck{};
    ack->hasAccepted = hasAccepted;
    ack->accepted = accepted;
}

struct StreamingSession {
    std::string token;
    fcitx::ICUUID uuid{};
    std::string frontend;
    std::string program;
    bool preeditCapable = false;
    // Last confirmed activity (begin / successful preedit update). The TTL
    // is measured from THIS, not from creation: a 3-minute dictation with
    // live preedit updates must not be hard-expired 120 s after Begin and
    // silently drop the tail of the utterance.
    std::chrono::steady_clock::time_point lastActive{};
    bool finished = false;
    // Next CommitSegment sequence. Starts at 1; only an accepted segment
    // advances it by one. The token stays live across those commits.
    uint32_t nextSegment = 1;
    SegmentAck ack{};
    // Last preedit text this session wrote into the context ("" once
    // cleared). Used to only ever remove preedit that is still ours.
    std::string lastPreedit;
};

bool isSensitive(const fcitx::InputContext *ic) {
    const auto flags = ic->capabilityFlags();
    return flags.test(fcitx::CapabilityFlag::Password) ||
           flags.test(fcitx::CapabilityFlag::Sensitive);
}

bool usableContext(const fcitx::InputContext *ic) {
    return ic != nullptr && ic->frontendName() != std::string_view("dummy") &&
           !isSensitive(ic);
}

std::string newToken() {
    static std::mutex randomMutex;
    static std::mt19937_64 rng{std::random_device{}()};
    static uint64_t counter = 0;
    uint64_t a;
    uint64_t b;
    {
        std::lock_guard<std::mutex> guard(randomMutex);
        a = rng();
        b = rng();
        counter += 1;
    }
    char buf[40];
    std::snprintf(buf, sizeof(buf), "%016llx%016llx%x",
                  static_cast<unsigned long long>(a),
                  static_cast<unsigned long long>(b),
                  static_cast<unsigned>(counter & 0xffff));
    return std::string(buf);
}

class RecordianCommitVTable
    : public fcitx::dbus::ObjectVTable<RecordianCommitVTable> {
public:
    explicit RecordianCommitVTable(fcitx::Instance *instance)
        : instance_(instance) {
        watch(fcitx::EventType::InputContextFocusOut);
        watch(fcitx::EventType::InputContextDestroyed);
        watch(fcitx::EventType::InputContextKeyEvent);
        watch(fcitx::EventType::InputContextCapabilityChanged);
        // Toolkit reset (mouse click in the same field, app-initiated reset),
        // caret/surrounding-text change, and manual input-method switch all
        // invalidate the bound session: the user moved the caret or changed
        // the editing state under our preedit.
        watch(fcitx::EventType::InputContextReset);
        watch(fcitx::EventType::InputContextSurroundingTextUpdated);
        watch(fcitx::EventType::InputContextSwitchInputMethod);
        // Posted by InputContext::commitString. Used as provenance that the
        // string we just asked to commit is the one the framework is sending.
        watch(fcitx::EventType::InputContextCommitString);
    }

    std::string Ping() { return "ok"; }

    std::string CommitText(const std::string &text) {
        auto *ic = instance_->lastFocusedInputContext();
        if (ic == nullptr || !ic->hasFocus() || !usableContext(ic)) {
            throw fcitx::dbus::MethodCallError(
                kError, "no focused non-sensitive input context (org.fcitx.Fcitx.Recordian.Error.NoInputContext)");
        }
        ic->reset();
        ic->commitString(text);
        return std::string(ic->frontendName()) + " " + ic->program();
    }

    std::string BeginSession(const std::string &initialPreedit) {
        auto *ic = instance_->lastFocusedInputContext();
        if (ic == nullptr || !ic->hasFocus() || !usableContext(ic)) {
            throw fcitx::dbus::MethodCallError(
                kError, "no focused non-sensitive input context to bind (org.fcitx.Fcitx.Recordian.Error.NoInputContext)");
        }
        const auto uuid = ic->uuid();
        std::vector<std::shared_ptr<StreamingSession>> replaced;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            replaced = dropExpiredLocked();
            // Refuse to overwrite a preedit that no live Recordian session
            // owns *and still shows exactly*: the user is composing with
            // their own IME (Rime, pinyin, ...). Matching lastPreedit
            // against the context's current client preedit also refuses the
            // case where a foreign IME replaced our preedit meanwhile.
            const std::string existingClient = currentPreeditText(ic);
            const bool ownedByUs =
                ownsCurrentPreeditLocked(uuid, existingClient);
            const std::string existingServer =
                ic->inputPanel().preedit().toString();
            if (!existingServer.empty() ||
                (!existingClient.empty() && !ownedByUs)) {
                throw fcitx::dbus::MethodCallError(
                    kErrorPreedit,
                    "input context already has a non-empty preedit (org.fcitx.Fcitx.Recordian.Error.ExistingPreedit)");
            }
            // A new session on the same context supersedes the old one.
            // Superseded entries are ERASED from the table (capacity counts
            // live sessions only — a burst of Begins on one focused context
            // must not exhaust kMaxSessions with dead entries) while the
            // shared_ptr is kept so the preedit it still owns can be
            // cleared below.
            for (auto it = sessions_.begin(); it != sessions_.end();) {
                if (it->second->uuid == uuid) {
                    it->second->finished = true;
                    replaced.push_back(it->second);
                    it = sessions_.erase(it);
                } else {
                    ++it;
                }
            }
            if (sessions_.size() >= kMaxSessions) {
                throw fcitx::dbus::MethodCallError(
                    kErrorBusy, "too many concurrent streaming sessions (org.fcitx.Fcitx.Recordian.Error.SessionBusy)");
            }
        }
        // Clean any preedit a superseded/expired session still owns, even
        // when the new session itself cannot render inline (no residue).
        // Each entry is resolved through its OWN uuid: an expired session
        // may belong to a different context than the one being bound now.
        for (const auto &entry : replaced) {
            clearOwnedPreedit(
                instance_->inputContextManager().findByUUID(entry->uuid),
                entry);
        }
        StreamingSession session;
        session.token = newToken();
        session.uuid = uuid;
        session.frontend = std::string(ic->frontendName());
        session.program = ic->program();
        session.preeditCapable =
            ic->capabilityFlags().test(fcitx::CapabilityFlag::Preedit);
        session.lastActive = std::chrono::steady_clock::now();
        const std::string token = session.token;
        const bool preeditCapable = session.preeditCapable;
        const std::string frontend = session.frontend;
        const std::string program = session.program;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            sessions_[token] =
                std::make_shared<StreamingSession>(std::move(session));
        }
        if (preeditCapable) {
            // Replaces any preedit a superseded Recordian session left.
            setClientPreedit(ic, initialPreedit);
            auto entry = findSession(token);
            if (entry != nullptr) {
                entry->lastPreedit = initialPreedit;
            }
        }
        return token + " preedit=" + (preeditCapable ? "1" : "0") +
               " frontend=" + frontend + " program=" + program + " segments=1";
    }

    std::string CommitSegment(const std::string &token, uint32_t sequence,
                              const std::string &text) {
        std::shared_ptr<StreamingSession> entry;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            auto it = sessions_.find(token);
            if (it == sessions_.end()) {
                throw fcitx::dbus::MethodCallError(
                    kErrorStale,
                    "unknown or stale session (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
            entry = it->second;
            if (entry->finished) {
                throw fcitx::dbus::MethodCallError(
                    kErrorStale,
                    "session already committed or cancelled (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
            // Reject before any write and before consuming the token.
            // Duplicate (sequence already accepted) and skip (sequence
            // jumped ahead) both leave nextSegment unchanged.
            if (sequence != entry->nextSegment ||
                entry->nextSegment == std::numeric_limits<uint32_t>::max()) {
                throw fcitx::dbus::MethodCallError(
                    kErrorSequence,
                    "duplicate or out-of-order segment (org.fcitx.Fcitx.Recordian.Error.BadSequence)");
            }
        }
        auto *ic = resolveForEntry(entry, /*requireFocus=*/true);
        if (ic == nullptr) {
            invalidateSession(token);
            clearOwnedPreedit(instance_->inputContextManager().findByUUID(
                                  entry->uuid),
                              entry);
            throw fcitx::dbus::MethodCallError(
                kErrorStale,
                "bound input context lost focus or is gone (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        if (foreignPreeditAppeared(ic, entry)) {
            invalidateSession(token);
            throw fcitx::dbus::MethodCallError(
                kErrorStale,
                "preedit was replaced by another source (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        {
            std::lock_guard<std::mutex> guard(mutex_);
            auto it = sessions_.find(token);
            if (it == sessions_.end() || it->second.get() != entry.get() ||
                entry->finished) {
                throw fcitx::dbus::MethodCallError(
                    kErrorStale,
                    "session already committed or cancelled (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
            if (sequence != entry->nextSegment) {
                throw fcitx::dbus::MethodCallError(
                    kErrorSequence,
                    "duplicate or out-of-order segment (org.fcitx.Fcitx.Recordian.Error.BadSequence)");
            }
            // Advance before the toolkit write so a lost reply cannot be
            // retried into a second commit of this sequence. The token
            // stays in the map; only CommitSession / Cancel consumes it.
            entry->nextSegment = sequence + 1;
            entry->lastActive = std::chrono::steady_clock::now();
            if (text.empty()) {
                clearPendingAck(&entry->ack);
            } else {
                // Predict the client echo from the surrounding text BEFORE
                // this write. cursor/anchor are Unicode offsets.
                SegmentAck ack;
                ack.armed = true;
                ack.sequence = sequence;
                ack.uuid = entry->uuid;
                ack.requested = text;
                ack.before = readSurround(ic);
                ack.afterKnown = predictCommittedSurround(ack.before, text, &ack.after);
                ack.hasAccepted = entry->ack.hasAccepted;
                ack.accepted = entry->ack.accepted;
                entry->ack = std::move(ack);
            }
        }
        clearOwnedPreedit(ic, entry);
        if (text.empty()) {
            return "segment " + std::to_string(sequence) + " cleared";
        }
        {
            // Nested CommitString during this call is ours. A different
            // string from commitFilter poisons the prediction.
            struct Depth {
                int &value;
                explicit Depth(int &value) : value(value) { value += 1; }
                ~Depth() { value -= 1; }
            } guard(selfCommitDepth_);
            ic->commitString(text);
        }
        {
            std::lock_guard<std::mutex> guard(mutex_);
            auto it = sessions_.find(token);
            if (it == sessions_.end() || it->second->finished ||
                it->second->ack.poisoned) {
                throw fcitx::dbus::MethodCallError(
                    kErrorStale,
                    "bound input context changed during segment commit (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
        }
        return "segment " + std::to_string(sequence) + " " +
               std::string(ic->frontendName()) + " " + ic->program();
    }

    std::string UpdatePreedit(const std::string &token,
                              const std::string &text) {
        auto entry = findSession(token);
        if (entry == nullptr) {
            throw fcitx::dbus::MethodCallError(
                kErrorStale,
                "unknown session (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        if (entry->finished) {
            throw fcitx::dbus::MethodCallError(
                kErrorStale, "session already committed or cancelled (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        // Validate focus / TTL for preview-only sessions too: "nothing to
        // render" must not silently keep a session whose context lost focus
        // or expired.
        auto *ic = resolveForEntry(entry, /*requireFocus=*/true);
        if (ic == nullptr) {
            // Fail closed: never touch an unresolvable context, and make
            // sure the stale session cannot be used afterwards.
            invalidateSession(token);
            throw fcitx::dbus::MethodCallError(
                kErrorStale, "bound input context lost focus or is gone (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        if (!entry->preeditCapable) {
            // Preview-only client: nothing to render inline, but the call
            // itself proves the session is still alive — refresh the
            // activity clock so a long preview-only dictation is not
            // expired 120 s after Begin.
            entry->lastActive = std::chrono::steady_clock::now();
            return "noop_preview_only";
        }
        if (foreignPreeditAppeared(ic, entry)) {
            invalidateSession(token);
            throw fcitx::dbus::MethodCallError(
                kErrorStale, "preedit was replaced by another source (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        setClientPreedit(ic, text);
        entry->lastPreedit = text;
        entry->lastActive = std::chrono::steady_clock::now();
        return "updated";
    }

    std::string CommitSession(const std::string &token,
                              const std::string &text) {
        std::shared_ptr<StreamingSession> entry;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            auto it = sessions_.find(token);
            if (it == sessions_.end()) {
                throw fcitx::dbus::MethodCallError(
                kErrorStale,
                "unknown or stale session (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
            entry = it->second;
            if (entry->finished) {
                throw fcitx::dbus::MethodCallError(
                    kErrorStale, "session already committed or cancelled (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
            }
        }
        // Resolve *before* consuming the session: an unfocused or destroyed
        // context must fail the commit without writing anywhere.
        auto *ic = resolveForEntry(entry, /*requireFocus=*/true);
        if (ic == nullptr) {
            invalidateSession(token);
            clearOwnedPreedit(instance_->inputContextManager().findByUUID(
                                  entry->uuid),
                              entry);
            throw fcitx::dbus::MethodCallError(
                kErrorStale, "bound input context lost focus or is gone (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        if (foreignPreeditAppeared(ic, entry)) {
            // The user (or another IME) replaced our preedit with their own
            // composition. Committing on top of it would splice two
            // compositions; fail the session instead, leaving their preedit
            // untouched.
            invalidateSession(token);
            throw fcitx::dbus::MethodCallError(
                kErrorStale, "preedit was replaced by another source (org.fcitx.Fcitx.Recordian.Error.StaleSession)");
        }
        {
            std::lock_guard<std::mutex> guard(mutex_);
            // Consume exactly once: duplicate commits cannot repeat.
            entry->finished = true;
            sessions_.erase(token);
        }
        clearOwnedPreedit(ic, entry);
        if (text.empty()) {
            return "cleared";
        }
        ic->commitString(text);
        return std::string("committed ") + std::string(ic->frontendName()) +
               " " + ic->program();
    }

    std::string CancelSession(const std::string &token) {
        std::shared_ptr<StreamingSession> entry;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            auto it = sessions_.find(token);
            if (it == sessions_.end()) {
                return "already_gone";
            }
            entry = it->second;
            entry->finished = true;
            clearPendingAck(&entry->ack);
            sessions_.erase(it);
        }
        // Resolve through the *kept* entry: the session is gone from the
        // map but the UUID still identifies the context whose preedit we
        // must clear. If the context died, there is nothing to clean up.
        auto *ic = instance_->inputContextManager().findByUUID(entry->uuid);
        clearOwnedPreedit(ic, entry);
        return "cancelled";
    }

private:
    void watch(fcitx::EventType type) {
        handlers_.push_back(instance_->watchEvent(
            type, fcitx::EventWatcherPhase::Default,
            [this](fcitx::Event &event) { handleEvent(event); }));
    }

    void handleEvent(fcitx::Event &event) {
        auto *icEvent = dynamic_cast<fcitx::InputContextEvent *>(&event);
        if (icEvent == nullptr) {
            return;
        }
        const auto uuid = icEvent->inputContext()->uuid();
        std::vector<std::shared_ptr<StreamingSession>> dropped;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            for (auto it = sessions_.begin(); it != sessions_.end();) {
                auto &session = *it->second;
                bool drop = false;
                switch (event.type()) {
                case fcitx::EventType::InputContextCommitString: {
                    // Provenance for the commit we just issued. Only the
                    // nested event from CommitSegment may arm the echo.
                    if (session.uuid != uuid || !session.ack.armed ||
                        selfCommitDepth_ <= 0) {
                        break;
                    }
                    auto *commitEvent =
                        dynamic_cast<fcitx::CommitStringEvent *>(&event);
                    if (commitEvent == nullptr ||
                        commitEvent->text() != session.ack.requested ||
                        session.ack.uuid != uuid ||
                        session.ack.sequence == 0) {
                        session.ack.poisoned = true;
                        session.ack.afterKnown = false;
                        clearPendingAck(&session.ack);
                        drop = true;
                        break;
                    }
                    session.ack.commitSeen = true;
                    session.ack.afterKnown = predictCommittedSurround(
                        session.ack.before, commitEvent->text(), &session.ack.after);
                    // No before-snapshot: the following surrounding event
                    // may accept only the exact empty-field insert below.
                    break;
                }
                case fcitx::EventType::InputContextSurroundingTextUpdated:
                    if (session.uuid != uuid) {
                        break;
                    }
                    drop = !surroundingIsOwnCommit(session, icEvent->inputContext());
                    if (drop) {
                        clearPendingAck(&session.ack);
                    }
                    break;
                case fcitx::EventType::InputContextFocusOut:
                case fcitx::EventType::InputContextDestroyed:
                case fcitx::EventType::InputContextReset:
                case fcitx::EventType::InputContextSwitchInputMethod:
                    // Focus loss, context destruction, toolkit reset (mouse
                    // click / app reset), and manual IM switch all age out
                    // the session. Reset is never treated as our commit echo.
                    if (session.uuid == uuid) {
                        clearPendingAck(&session.ack);
                        drop = true;
                    }
                    break;
                case fcitx::EventType::InputContextKeyEvent: {
                    // Typing, navigation, and chords (Ctrl+A) invalidate:
                    // the user is editing over our preedit. A bare modifier
                    // press does not change text. Key::isModifier() on
                    // Fcitx 5.1.7 is true only for Shift/Control/Meta/Alt/
                    // Super/Hyper left or right keysyms, state bits ignored,
                    // so Control_R stays exempt even if Ctrl is already set,
                    // while Ctrl+A and arrows do not.
                    auto *keyEvent =
                        dynamic_cast<fcitx::KeyEvent *>(&event);
                    const bool modifierOnly =
                        keyEvent != nullptr && keyEvent->key().isModifier();
                    drop = (session.uuid == uuid) && keyEvent != nullptr &&
                           !keyEvent->isRelease() && !modifierOnly;
                    break;
                }
                case fcitx::EventType::InputContextCapabilityChanged:
                    drop = (session.uuid == uuid) &&
                           isSensitive(icEvent->inputContext());
                    break;
                default:
                    break;
                }
                if (drop) {
                    session.finished = true;
                    dropped.push_back(it->second);
                    it = sessions_.erase(it);
                } else {
                    ++it;
                }
            }
        }
        if (event.type() == fcitx::EventType::InputContextDestroyed) {
            // The context is going away; touching it now is unsafe.
            return;
        }
        for (const auto &session : dropped) {
            // Remove residue: clear only preedit that is still exactly what
            // this session wrote — if the user's IME replaced it meanwhile,
            // it is theirs now.
            clearOwnedPreedit(icEvent->inputContext(), session);
        }
    }

    // Erase sessions past their TTL and return them so the caller can clear
    // any preedit they still own (never leave expired preedit behind).
    std::vector<std::shared_ptr<StreamingSession>> dropExpiredLocked() {
        std::vector<std::shared_ptr<StreamingSession>> expired;
        const auto now = std::chrono::steady_clock::now();
        for (auto it = sessions_.begin(); it != sessions_.end();) {
            if (now - it->second->lastActive > kSessionTTL) {
                it->second->finished = true;
                clearPendingAck(&it->second->ack);
                expired.push_back(it->second);
                it = sessions_.erase(it);
            } else {
                ++it;
            }
        }
        return expired;
    }

    void invalidateSession(const std::string &token) {
        auto it = sessions_.find(token);
        if (it == sessions_.end()) {
            return;
        }
        it->second->finished = true;
        clearPendingAck(&it->second->ack);
        sessions_.erase(it);
    }

    // True only for the predicted echo of this session's own CommitSegment,
    // or a repeat of that already-accepted snapshot. Any other surrounding
    // text or caret is a user/programmatic edit.
    bool surroundingIsOwnCommit(StreamingSession &session,
                                const fcitx::InputContext *ic) {
        if (session.ack.poisoned) {
            return false;
        }
        const SurroundSnap observed = readSurround(ic);
        if (session.ack.armed) {
            if (session.ack.uuid != session.uuid) {
                return false;
            }
            if (session.ack.afterKnown &&
                sameSnap(observed, session.ack.after)) {
                session.ack.hasAccepted = true;
                session.ack.accepted = observed;
                clearPendingAck(&session.ack);
                return true;
            }
            // Client repeated the previous snapshot and has not applied
            // this insert yet. Keep waiting; do not treat it as an edit.
            if (session.ack.hasAccepted &&
                sameSnap(observed, session.ack.accepted)) {
                return true;
            }
            // First insert into a context that has never reported
            // surrounding text. commitSeen proves the string is the one
            // CommitSegment just passed to commitString. The snapshot
            // matching that string, with the caret at its end, is the
            // empty-before echo. Anything longer, different, or not at
            // the end stays a mismatch (foreign prefix, other caret).
            if (session.ack.commitSeen && !session.ack.before.valid &&
                !session.ack.requested.empty() && observed.valid &&
                observed.text == session.ack.requested &&
                caretAtUtf8End(observed)) {
                session.ack.hasAccepted = true;
                session.ack.accepted = observed;
                clearPendingAck(&session.ack);
                return true;
            }
            FCITX_WARN() << "Recordian CommitSegment echo mismatch seq="
                         << session.ack.sequence
                         << " commitSeen=" << session.ack.commitSeen
                         << " beforeValid=" << session.ack.before.valid
                         << " beforeCursor=" << session.ack.before.cursor
                         << " beforeAnchor=" << session.ack.before.anchor
                         << " afterKnown=" << session.ack.afterKnown
                         << " afterCursor=" << session.ack.after.cursor
                         << " afterAnchor=" << session.ack.after.anchor
                         << " obsValid=" << observed.valid
                         << " obsCursor=" << observed.cursor
                         << " obsAnchor=" << observed.anchor
                         << " beforeBytes=" << session.ack.before.text.size()
                         << " afterBytes=" << session.ack.after.text.size()
                         << " obsBytes=" << observed.text.size();
            return false;
        }
        return session.ack.hasAccepted && sameSnap(observed, session.ack.accepted);
    }

    std::shared_ptr<StreamingSession> findSession(const std::string &token) {
        std::lock_guard<std::mutex> guard(mutex_);
        auto it = sessions_.find(token);
        if (it == sessions_.end()) {
            return nullptr;
        }
        return it->second;
    }

    bool ownsCurrentPreeditLocked(const fcitx::ICUUID &uuid,
                                  const std::string &currentPreedit) {
        if (currentPreedit.empty()) {
            return false;
        }
        for (const auto &entry : sessions_) {
            if (entry.second->uuid == uuid && !entry.second->finished &&
                entry.second->lastPreedit == currentPreedit) {
                return true;
            }
        }
        return false;
    }

    fcitx::InputContext *resolveForEntry(
        const std::shared_ptr<StreamingSession> &entry, bool requireFocus) {
        if (entry == nullptr || entry->finished) {
            return nullptr;
        }
        if (std::chrono::steady_clock::now() - entry->lastActive > kSessionTTL) {
            // Expired sessions resolve as stale so TTL is enforced on every
            // call, not only when BeginSession sweeps the map. Erase under
            // the lock, then clear the preedit this session still owns
            // OUTSIDE the lock (updatePreedit on the InputContext may run
            // frontend callbacks that could re-enter this vtable). Only the
            // session's own exact preedit is cleared — a preedit the user's
            // IME replaced in the meantime is theirs and stays.
            {
                std::lock_guard<std::mutex> guard(mutex_);
                for (auto it = sessions_.begin(); it != sessions_.end();) {
                    if (it->second == entry) {
                        it->second->finished = true;
                        clearPendingAck(&it->second->ack);
                        it = sessions_.erase(it);
                    } else {
                        ++it;
                    }
                }
            }
            clearOwnedPreedit(
                instance_->inputContextManager().findByUUID(entry->uuid),
                entry);
            return nullptr;
        }
        auto *ic = instance_->inputContextManager().findByUUID(entry->uuid);
        if (ic == nullptr || !usableContext(ic)) {
            return nullptr;
        }
        if (requireFocus && !ic->hasFocus()) {
            return nullptr;
        }
        return ic;
    }

    static std::string currentPreeditText(const fcitx::InputContext *ic) {
        if (ic == nullptr) {
            return std::string();
        }
        return ic->inputPanel().clientPreedit().toString();
    }

    static bool foreignPreeditAppeared(
        const fcitx::InputContext *ic,
        const std::shared_ptr<StreamingSession> &entry) {
        // Someone else wrote a non-empty preedit since our last update
        // (user switched to an IME composition). Never overwrite that.
        const std::string current = currentPreeditText(ic);
        if (current.empty()) {
            return false;
        }
        return current != entry->lastPreedit;
    }

    static void setClientPreedit(fcitx::InputContext *ic,
                                 const std::string &text) {
        fcitx::Text preedit(text);
        preedit.setCursor(static_cast<int>(text.size()));
        ic->inputPanel().setClientPreedit(std::move(preedit));
        ic->updatePreedit();
    }

    static void clearOwnedPreedit(fcitx::InputContext *ic,
                                  const std::shared_ptr<StreamingSession>
                                      &entry) {
        if (ic == nullptr || entry == nullptr) {
            return;
        }
        if (entry->lastPreedit.empty()) {
            return;
        }
        const std::string current = currentPreeditText(ic);
        if (current != entry->lastPreedit) {
            // The preedit was replaced (user's IME took over): leave it.
            return;
        }
        // Removing exactly the text this session wrote is always safe, even
        // when the context turned sensitive meanwhile (usableContext would
        // reject it and leave our own preedit stuck in a password field).
        ic->inputPanel().setClientPreedit(fcitx::Text());
        ic->updatePreedit();
        entry->lastPreedit.clear();
    }

    fcitx::Instance *instance_;
    // Non-zero only while CommitSegment is inside InputContext::commitString.
    int selfCommitDepth_ = 0;
    std::mutex mutex_;
    std::unordered_map<std::string, std::shared_ptr<StreamingSession>>
        sessions_;
    std::vector<std::unique_ptr<fcitx::HandlerTableEntry<
        fcitx::EventHandler>>>
        handlers_;

    FCITX_OBJECT_VTABLE_METHOD(Ping, "Ping", "", "s");
    FCITX_OBJECT_VTABLE_METHOD(CommitText, "CommitText", "s", "s");
    FCITX_OBJECT_VTABLE_METHOD(BeginSession, "BeginSession", "s", "s");
    FCITX_OBJECT_VTABLE_METHOD(UpdatePreedit, "UpdatePreedit", "ss", "s");
    FCITX_OBJECT_VTABLE_METHOD(CommitSegment, "CommitSegment", "sus", "s");
    FCITX_OBJECT_VTABLE_METHOD(CommitSession, "CommitSession", "ss", "s");
    FCITX_OBJECT_VTABLE_METHOD(CancelSession, "CancelSession", "s", "s");
};

class RecordianCommitModule : public fcitx::AddonInstance {
public:
    explicit RecordianCommitModule(fcitx::Instance *instance)
        : instance_(instance), vtable_(instance) {
        auto *dbusAddon = instance_->addonManager().addon("dbus", true);
        if (dbusAddon == nullptr) {
            throw std::runtime_error("fcitx dbus addon is not loaded");
        }
        auto *bus = dbusAddon->call<fcitx::IDBusModule::bus>();
        if (bus == nullptr || !bus->addObjectVTable(kPath, kInterface, vtable_)) {
            throw std::runtime_error("failed to export Recordian commit interface");
        }
    }

private:
    fcitx::Instance *instance_;
    RecordianCommitVTable vtable_;
};

class RecordianCommitFactory : public fcitx::AddonFactory {
public:
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new RecordianCommitModule(manager->instance());
    }
};

}  // namespace

FCITX_ADDON_FACTORY(RecordianCommitFactory)
