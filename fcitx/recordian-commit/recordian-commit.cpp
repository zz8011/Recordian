/*
 * Fcitx5 module: commit an external string into the focused input context.
 *
 * Recordian calls org.fcitx.Fcitx.Recordian1.CommitText over the session bus.
 * This does not go through Rime, and it does not update the Rime user dictionary.
 */
#include <stdexcept>
#include <string>
#include <string_view>

#include <fcitx-utils/dbus/objectvtable.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontext.h>
#include <fcitx/instance.h>

#include "dbus_public.h"

namespace {

constexpr char kPath[] = "/recordian";
constexpr char kInterface[] = "org.fcitx.Fcitx.Recordian1";
constexpr char kError[] = "org.fcitx.Fcitx.Recordian.Error.NoInputContext";

class RecordianCommitVTable : public fcitx::dbus::ObjectVTable<RecordianCommitVTable> {
public:
    explicit RecordianCommitVTable(fcitx::Instance *instance) : instance_(instance) {}

    std::string Ping() { return "ok"; }

    std::string CommitText(const std::string &text) {
        fcitx::InputContext *ic = instance_->lastFocusedInputContext();
        if (ic == nullptr || !ic->hasFocus() || ic->frontendName() == "dummy") {
            ic = instance_->mostRecentInputContext();
        }
        if (ic == nullptr || ic->frontendName() == "dummy") {
            throw fcitx::dbus::MethodCallError(kError, "no focused input context");
        }
        if (ic->hasFocus()) {
            ic->reset();
        }
        ic->commitString(text);
        return std::string(ic->frontendName()) + " " + ic->program();
    }

private:
    fcitx::Instance *instance_;

    FCITX_OBJECT_VTABLE_METHOD(Ping, "Ping", "", "s");
    FCITX_OBJECT_VTABLE_METHOD(CommitText, "CommitText", "s", "s");
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
