// Exercise the actual bridge policy without starting a desktop or an IME.
#include "../../fcitx/recordian-commit/recordian-commit.cpp"
#include <cassert>

int main() {
    StreamingSession session;
    session.frontend = "wayland_v2";
    const SurroundSnap unknown{};
    const SurroundSnap initial{true, "草稿\n", 2, 2};
    initializeSurroundingBaseline(session, {true, "上一轮输入框缓存", 0, 0});
    assert(!session.ack.hasAccepted);
    assert(acceptPreeditSurrounding(session, unknown));
    assert(!session.ack.hasAccepted);
    assert(acceptPreeditSurrounding(session, initial));
    assert(session.ack.hasAccepted);
    for (int i = 0; i < 5; ++i) {
        assert(acceptPreeditSurrounding(session, initial));
    }
    assert(!acceptPreeditSurrounding(session, {true, "草稿\n", 1, 1}));
    assert(!acceptPreeditSurrounding(session, {true, "草稿\n", 2, 0}));
    assert(!acceptPreeditSurrounding(session, {true, "外部编辑", 2, 2}));
    assert(!acceptPreeditSurrounding(session, unknown));
    // Rejected changes never become a new baseline.
    assert(sameSnap(session.ack.accepted, initial));

    session.ack.armed = true;
    assert(!acceptPreeditSurrounding(session, initial));
    session.ack.armed = false;
    session.ack.poisoned = true;
    assert(!acceptPreeditSurrounding(session, initial));

    StreamingSession afterCommit;
    afterCommit.frontend = "wayland_v2";
    afterCommit.nextSegment = 2;
    assert(!acceptPreeditSurrounding(afterCommit, initial));
    StreamingSession gtk;
    gtk.frontend = "dbus";
    assert(!acceptPreeditSurrounding(gtk, initial));
    // All clients may repeat a valid baseline captured at BeginSession.
    initializeSurroundingBaseline(gtk, initial);
    assert(acceptPreeditSurrounding(gtk, initial));

    // Qt/DBus editors may never report surrounding text. Their own preedit
    // refreshes keep the bound token alive, but no segment can be proved.
    StreamingSession unknownDbus;
    unknownDbus.frontend = "dbus";
    unknownDbus.lastPreedit = "正在说话";
    assert(acceptPreeditSurrounding(unknownDbus, unknown));
    assert(acceptPreeditSurrounding(unknownDbus, unknown));
    assert(!acceptPreeditSurrounding(unknownDbus, initial));

    // Chromium's empty contenteditable may change its layout newlines
    // between utterances. The first fresh event wins over the old cache.
    StreamingSession emptyEditor;
    emptyEditor.frontend = "wayland_v2";
    initializeSurroundingBaseline(emptyEditor, {true, "\n", 0, 0});
    const SurroundSnap freshEmpty{true, "\n\n", 0, 0};
    assert(acceptPreeditSurrounding(emptyEditor, freshEmpty));
    assert(acceptPreeditSurrounding(emptyEditor, freshEmpty));
    assert(!acceptPreeditSurrounding(emptyEditor, {true, "\n\n", 1, 1}));

    // Real Codex diagnostics: one Han character + two layout newlines
    // (5 bytes, caret 1) becomes three Han characters + the same newlines
    // (11 bytes, caret 3). These are our changing composition, not user edits.
    StreamingSession chromium;
    chromium.frontend = "wayland_v2";
    chromium.lastPreedit = "我";
    rememberPreedit(chromium, "我");
    assert(acceptPreeditSurrounding(chromium, {true, "我\n\n", 1, 1}));
    chromium.lastPreedit = "我想说";
    rememberPreedit(chromium, "我想说");
    assert(acceptPreeditSurrounding(chromium, {true, "我想说\n\n", 3, 3}));
    assert(sameSnap(chromium.ack.accepted, {true, "\n\n", 0, 0}));
    chromium.lastPreedit = "我想试";
    rememberPreedit(chromium, "我想试");
    assert(acceptPreeditSurrounding(chromium, {true, "我想试\n\n", 3, 3}));
    // Delayed earlier echo, excluded-composition echo, and repeated callback.
    assert(acceptPreeditSurrounding(chromium, {true, "我想说\n\n", 3, 3}));
    assert(acceptPreeditSurrounding(chromium, {true, "\n\n", 0, 0}));
    const SurroundSnap live{true, "我想试\n\n", 3, 3};
    const auto baseline = commitBaseline(chromium, live);
    SurroundSnap final;
    assert(predictCommittedSurround(baseline, "我想试。", &final));
    assert(final.text == "我想试。\n\n"); // no duplicate composition
    assert(!acceptPreeditSurrounding(chromium, {true, "我想试\n\n", 2, 2}));
    assert(!acceptPreeditSurrounding(chromium, {true, "我想试\n\n", 3, 0}));
    assert(!acceptPreeditSurrounding(chromium, {true, "其他人\n\n", 3, 3}));
    assert(!acceptPreeditSurrounding(chromium, {true, "我想试X\n\n", 4, 4}));
    assert(!acceptPreeditSurrounding(chromium, unknown));

    StreamingSession switchesReporting;
    switchesReporting.frontend = "wayland_v2";
    switchesReporting.lastPreedit = "我";
    rememberPreedit(switchesReporting, "我");
    assert(acceptPreeditSurrounding(switchesReporting, {true, "我\n\n", 1, 1}));
    assert(acceptPreeditSurrounding(switchesReporting, {true, "\n\n", 0, 0}));
    assert(sameSnap(switchesReporting.ack.accepted, {true, "\n\n", 0, 0}));

    // Existing document content on both sides must be preserved exactly.
    StreamingSession middle;
    middle.frontend = "wayland_v2";
    assert(acceptPreeditSurrounding(middle, {true, "甲乙", 1, 1}));
    middle.lastPreedit = "中文🙂";
    rememberPreedit(middle, middle.lastPreedit);
    assert(acceptPreeditSurrounding(middle, {true, "甲中文🙂乙", 4, 4}));
    assert(!acceptPreeditSurrounding(middle, {true, "甲中文🙂丙", 4, 4}));
    assert(!acceptPreeditSurrounding(middle, {true, "中文🙂甲乙", 3, 3}));
    assert(sameSnap(commitBaseline(middle, {true, "甲中文🙂乙", 4, 4}),
                    {true, "甲乙", 1, 1}));

    // Empty paragraph placeholders can add/remove at most two newlines,
    // only at the same empty caret during this session's own composition.
    emptyEditor.lastPreedit = "文字";
    rememberPreedit(emptyEditor, "文字");
    assert(acceptPreeditSurrounding(emptyEditor, {true, "\n", 0, 0}));
    assert(acceptPreeditSurrounding(emptyEditor, {true, "文字\n\n", 2, 2}));
    assert(!acceptPreeditSurrounding(emptyEditor, {true, "\n\n\n", 0, 0}));
    assert(!acceptPreeditSurrounding(emptyEditor, {true, " ", 0, 0}));

    // Unrelated frontends do not inherit the Wayland exception.
    rememberPreedit(gtk, "文字");
    assert(!acceptPreeditSurrounding(gtk, {true, "草稿文字\n", 4, 4}));

    // Antigravity reports its composition in surrounding text with the
    // caret anchored at the start. A changing OWN preedit must stay live.
    StreamingSession antigravity;
    antigravity.frontend = "wayland_v2";
    antigravity.program = "antigravity";
    antigravity.lastPreedit = "登";
    rememberPreedit(antigravity, "登");
    assert(acceptPreeditSurrounding(antigravity,
                                    {true, "前登后", 1, 1}));
    antigravity.lastPreedit = "登录";
    rememberPreedit(antigravity, "登录");
    assert(acceptPreeditSurrounding(antigravity,
                                    {true, "前登录后", 1, 1}));
    assert(sameSnap(antigravity.ack.accepted, {true, "前后", 1, 1}));
    assert(!acceptPreeditSurrounding(antigravity,
                                     {true, "前登录改", 1, 1}));
    assert(!acceptPreeditSurrounding(antigravity,
                                     {true, "前登录后", 2, 2}));

    // A capped 4 KiB window may move its outer edges, but a local edit
    // around the caret cannot masquerade as that movement.
    const SurroundSnap clippedBefore{true,
        std::string(1800, 'a') + "登" + std::string(2200, 'b'), 1800, 1800};
    const SurroundSnap clippedAfter{true,
        std::string(1797, 'a') + "登录" + std::string(2200, 'b'), 1797, 1797};
    StreamingSession clipped;
    clipped.frontend = "wayland_v2";
    clipped.program = "antigravity";
    clipped.lastPreedit = "登";
    rememberPreedit(clipped, "登");
    assert(acceptPreeditSurrounding(clipped, clippedBefore));
    clipped.lastPreedit = "登录";
    rememberPreedit(clipped, "登录");
    assert(acceptPreeditSurrounding(clipped, clippedAfter));
    assert(!acceptPreeditSurrounding(clipped,
        {true, std::string(1797, 'a') + "登录X" +
                   std::string(2199, 'b'), 1797, 1797}));
}
