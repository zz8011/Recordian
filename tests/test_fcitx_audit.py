"""Run the real bridge methods on synthetic Fcitx contexts, without addons or D-Bus."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "fcitx/recordian-commit/recordian-commit.cpp"

# Only the bridge's private access label is exposed in the translation unit.
# This lets the fixture advance its clock; all writes/events use real Fcitx APIs.
HARNESS = r'''
#include <iostream>

void require(bool condition, const char *message) {
    if (!condition) { throw std::runtime_error(message); }
}

template <typename Call>
void expectError(Call call, const char *name) {
    try { call(); }
    catch (const fcitx::dbus::MethodCallError &error) {
        require(std::string(error.name()) == name, error.what());
        return;
    }
    throw std::runtime_error(std::string("expected error: ") + name);
}

class SyntheticIC : public fcitx::InputContext {
public:
    explicit SyntheticIC(fcitx::InputContextManager &manager)
        : InputContext(manager, "recordian-audit") { created(); }
    ~SyntheticIC() { destroy(); }
    const char *frontend() const override { return "dbus"; }
    std::string committed;
    unsigned writes = 0;
    std::function<void()> onPreedit;
    std::function<void()> onCommit;
protected:
    void commitStringImpl(const std::string &text) override {
        committed += text;
        ++writes;
        if (onCommit) { onCommit(); }
    }
    void deleteSurroundingTextImpl(int, unsigned) override {}
    void forwardKeyImpl(const fcitx::ForwardKeyEvent &) override {}
    void updatePreeditImpl() override { if (onPreedit) { onPreedit(); } }
};

std::string begin(RecordianCommitVTable &bridge, const std::string &text = "") {
    const auto descriptor = bridge.BeginSession(text);
    return descriptor.substr(0, descriptor.find(' '));
}

void echo(SyntheticIC &ic, const std::string &text, unsigned cursor,
          unsigned anchor) {
    ic.surroundingText().setText(text, cursor, anchor);
    ic.updateSurroundingText();
}

void staleWithoutWrite(RecordianCommitVTable &bridge, SyntheticIC &ic,
                       const std::string &token) {
    const auto before = ic.committed;
    const auto writes = ic.writes;
    expectError([&] { bridge.CommitSession(token, "MUST-NOT-WRITE"); }, kErrorStale);
    expectError([&] { bridge.CommitSegment(token, 3, "MUST-NOT-RETRY"); }, kErrorStale);
    require(ic.committed == before && ic.writes == writes, "stale session wrote text");
}

int main(int argc, char **argv) {
    try {
        require(argc == 2, "scenario required");
        const std::string scenario = argv[1];
        char name[] = "recordian-audit", disable[] = "--disable", all[] = "all";
        char *options[] = {name, disable, all, nullptr};
        fcitx::Instance instance(3, options);
        instance.initialize(); // Every addon is disabled: no real frontend or bus.
        SyntheticIC ic(instance.inputContextManager());
        ic.setCapabilityFlags(fcitx::CapabilityFlags{
            fcitx::CapabilityFlag::Preedit, fcitx::CapabilityFlag::SurroundingText});
        ic.surroundingText().setText("", 0, 0);
        ic.focusIn();
        RecordianCommitVTable bridge(&instance);

        if (scenario.starts_with("expiry-")) {
            const auto token = begin(bridge, "owned");
            bridge.findSession(token)->lastActive -= std::chrono::seconds(121);
            if (scenario == "expiry-foreign-client") {
                ic.inputPanel().setClientPreedit(fcitx::Text("foreign"));
                expectError([&] { begin(bridge); }, kErrorPreedit);
                require(ic.inputPanel().clientPreedit().toString() == "foreign",
                        "expiry erased foreign client preedit");
            } else if (scenario == "expiry-foreign-server") {
                ic.inputPanel().setPreedit(fcitx::Text("foreign"));
                expectError([&] { begin(bridge); }, kErrorPreedit);
                require(ic.inputPanel().preedit().toString() == "foreign",
                        "expiry erased foreign server preedit");
                require(ic.inputPanel().clientPreedit().empty(),
                        "expired owned client preedit survived refused Begin");
            } else if (scenario == "expiry-focus-during-cleanup") {
                ic.onPreedit = [&] { ic.focusOut(); };
                expectError([&] { begin(bridge); }, kError);
                require(bridge.sessions_.empty(), "expiry rebound an unfocused context");
            } else {
                require(scenario == "expiry-owned", "unknown expiry scenario");
                const auto next = begin(bridge, "new");
                require(next != token, "expired token was reused");
                require(ic.inputPanel().clientPreedit().toString() == "new",
                        "new preedit missing after expiry");
                bridge.CancelSession(next);
                require(ic.inputPanel().clientPreedit().empty(), "new cancel left residue");
            }
            require(bridge.CancelSession(token) == "already_gone", "expired token survived");
            require(ic.committed.empty(), "expiry committed text");
        } else if (scenario == "duplicate-provenance") {
            const auto token = begin(bridge);
            bool nested = false;
            ic.onCommit = [&] {
                if (!nested) { nested = true; ic.commitString("A"); }
            };
            expectError([&] { bridge.CommitSegment(token, 1, "A"); }, kErrorStale);
            require(ic.writes == 2, "fixture did not issue the unexpected nested write");
            staleWithoutWrite(bridge, ic, token);
        } else if (scenario == "pending-count-bound" || scenario == "pending-release") {
            const auto token = begin(bridge);
            for (unsigned n = 1; n <= 32; ++n) { bridge.CommitSegment(token, n, "x"); }
            if (scenario == "pending-count-bound") {
                expectError([&] { bridge.CommitSegment(token, 33, "OVERFLOW"); },
                            kErrorSegmentsUnsafe);
                require(ic.writes == 32, "queue overflow wrote text");
                require(bridge.findSession(token)->nextSegment == 33,
                        "queue overflow consumed sequence");
                require(bridge.CommitSession(token, "TAIL").starts_with("committed "),
                        "bounded refusal prevented final commit");
                require(ic.committed == std::string(32, 'x') + "TAIL", "bounded final duplicated");
                staleWithoutWrite(bridge, ic, token);
            } else {
                echo(ic, std::string(16, 'x'), 16, 16);
                bridge.CommitSegment(token, 33, "Y");
                echo(ic, std::string(32, 'x') + "Y", 33, 33);
                require(bridge.findSession(token) && !bridge.findSession(token)->ack.armed,
                        "partial ack did not release queue capacity");
                bridge.CancelSession(token);
            }
        } else if (scenario == "pending-byte-bound") {
            const std::string original(2 * 1024 * 1024 - 1, 'x');
            ic.surroundingText().setText(original, original.size(), original.size());
            const auto token = begin(bridge);
            bridge.CommitSegment(token, 1, "A");
            expectError([&] { bridge.CommitSegment(token, 2, "B"); }, kErrorSegmentsUnsafe);
            require(ic.committed == "A" && ic.writes == 1, "byte overflow wrote text");
            require(bridge.findSession(token)->nextSegment == 2, "byte overflow consumed sequence");
            bridge.CancelSession(token);
        } else {
            if (scenario == "unicode-selection") {
                ic.surroundingText().setText("甲旧乙", 2, 1);
            }
            const auto token = begin(bridge);
            const bool unicode = scenario == "unicode-selection";
            bridge.CommitSegment(token, 1, unicode ? "中🙂" : "A");
            auto earlyEcho = instance.watchEvent(
                fcitx::EventType::InputContextCommitString, fcitx::EventWatcherPhase::PreInputMethod,
                [&](fcitx::Event &event) {
                    auto *commit = dynamic_cast<fcitx::CommitStringEvent *>(&event);
                    if (scenario == "early-surround-event" && commit && commit->text() == "B") {
                        // A nested frontend callback confirms A before the
                        // bridge observes the provenance event for B.
                        echo(ic, "A", 1, 1);
                    }
                });
            if (scenario == "empty-between") { bridge.CommitSegment(token, 2, ""); }
            bridge.CommitSegment(token, scenario == "empty-between" ? 3 : 2,
                                 unicode ? "文" : "B");
            if (scenario == "steps" || scenario == "empty-between" || scenario == "early-surround-event") {
                echo(ic, "A", 1, 1);
                require(bridge.findSession(token) != nullptr, "first delayed echo retired token");
                echo(ic, "A", 1, 1); // An already accepted snapshot may repeat.
                echo(ic, "AB", 2, 2);
            } else if (scenario == "coalesced" || scenario == "old-after-coalesced") {
                echo(ic, "AB", 2, 2);
                require(bridge.findSession(token) != nullptr, "coalesced echo retired token");
                if (scenario == "old-after-coalesced") {
                    echo(ic, "A", 1, 1);
                    staleWithoutWrite(bridge, ic, token);
                    std::cout << "PASS " << scenario << '\n';
                    return 0;
                }
            } else if (unicode) {
                echo(ic, "甲中🙂乙", 3, 3);
                require(bridge.findSession(token) != nullptr, "Unicode selection echo retired token");
                echo(ic, "甲中🙂文乙", 4, 4);
            } else if (scenario == "partial-then-more") {
                echo(ic, "A", 1, 1);
                bridge.CommitSegment(token, 3, "C");
                echo(ic, "AB", 2, 2);
                echo(ic, "ABC", 3, 3);
            } else {
                if (scenario == "manual-text") { echo(ic, "AX", 2, 2); }
                else if (scenario == "manual-caret") { echo(ic, "AB", 1, 1); }
                else if (scenario == "manual-selection") { echo(ic, "AB", 2, 0); }
                else if (scenario == "invalid-surround") {
                    ic.surroundingText().invalidate(); ic.updateSurroundingText();
                } else if (scenario == "manual-key") {
                    fcitx::KeyEvent key(&ic, fcitx::Key("a")); ic.keyEvent(key);
                } else if (scenario == "focus") { ic.focusOut(); }
                else if (scenario == "reset") { ic.reset(); }
                else if (scenario == "sensitive") {
                    ic.setCapabilityFlags(fcitx::CapabilityFlags{fcitx::CapabilityFlag::Password});
                } else if (scenario == "external-preedit") {
                    ic.inputPanel().setClientPreedit(fcitx::Text("foreign"));
                } else { throw std::runtime_error("unknown scenario"); }
                staleWithoutWrite(bridge, ic, token);
                if (scenario == "external-preedit") {
                    require(ic.inputPanel().clientPreedit().toString() == "foreign",
                            "stale cleanup erased foreign preedit");
                }
                require(ic.committed == "AB" && ic.writes == 2, "conflict wrote extra text");
                std::cout << "PASS " << scenario << '\n';
                return 0;
            }
            require(bridge.findSession(token) && !bridge.findSession(token)->ack.armed,
                    "all known echoes did not discharge pending state");
            expectError([&] { bridge.CommitSegment(token, 1, "RETRY"); }, kErrorSequence);
            require(bridge.CommitSession(token, "TAIL").starts_with("committed "),
                    "confirmed sequence lost final tail");
            const auto expected = unicode ? "中🙂文TAIL" :
                scenario == "partial-then-more" ? "ABCTAIL" : "ABTAIL";
            require(ic.committed == expected && ic.writes == (scenario == "partial-then-more" ? 4u : 3u),
                    "tail or segment was lost/duplicated");
            staleWithoutWrite(bridge, ic, token);
        }
        std::cout << "PASS " << scenario << '\n';
    } catch (const std::exception &error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
'''


@pytest.fixture(scope="module")
def synthetic_bridge(tmp_path_factory):
    if not shutil.which("g++") or not shutil.which("pkg-config"):
        pytest.skip("C++ compiler and Fcitx development files required")
    flags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "Fcitx5Core"], capture_output=True, text=True, check=False,
    )
    if flags.returncode:
        pytest.skip("Fcitx development files required")
    source = BRIDGE.read_text(encoding="utf-8")
    label = "\nprivate:\n    void watch("
    assert source.count(label) == 1
    source = source.replace(label, "\npublic:\n    void watch(", 1)
    executable = tmp_path_factory.mktemp("fcitx-audit-build") / "bridge-fixture"
    compile_result = subprocess.run(
        ["g++", "-std=c++20", "-x", "c++", "-", "-I", str(BRIDGE.parent), "-o", str(executable),
         *shlex.split(flags.stdout)],
        input=source + HARNESS, capture_output=True, text=True, timeout=60, check=False,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    return executable


@pytest.mark.parametrize("scenario", [
    "expiry-owned", "expiry-foreign-client", "expiry-foreign-server", "expiry-focus-during-cleanup",
    "steps", "coalesced", "unicode-selection", "partial-then-more", "empty-between", "old-after-coalesced",
    "early-surround-event", "duplicate-provenance",
    "manual-text", "manual-caret", "manual-selection", "invalid-surround", "manual-key", "focus", "reset",
    "sensitive", "external-preedit", "pending-count-bound", "pending-byte-bound", "pending-release",
])
def test_synthetic_fcitx_session_guards(synthetic_bridge, tmp_path, scenario):
    environment = dict(os.environ)
    for key, directory in {
        "HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
        "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "run",
    }.items():
        path = tmp_path / directory
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    environment.update(DBUS_SESSION_BUS_ADDRESS="unix:path=/dev/null", DISPLAY="", WAYLAND_DISPLAY="")
    result = subprocess.run(
        [str(synthetic_bridge), scenario], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == f"PASS {scenario}"
