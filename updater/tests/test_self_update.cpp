#include "self_update.h"
#include "test_support.h"
#include "util.h"
#include "workflow.h"
#include <cstring>

namespace tests {
using namespace updater;
namespace {
void Image(const fs::path& path, const std::string& marker) {
    fs::create_directories(path.parent_path());
    std::wstring executable(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, executable.data(), static_cast<DWORD>(executable.size()));
    CHECK(length > 0 && length < executable.size());
    CHECK(CopyFileW(executable.c_str(), path.c_str(), FALSE));
    std::ofstream append(path, std::ios::binary | std::ios::app);
    append << marker;
    CHECK(append.good());
}

std::vector<fs::path> Retired(const fs::path& install) {
    std::vector<fs::path> found;
    for (const auto& entry : fs::directory_iterator(install / L"Updater")) {
        if (entry.path().filename().wstring().find(L"updater.exe.old_update") == 0)
            found.push_back(entry.path());
    }
    return found;
}
}

void AddSelfUpdateTests(TestList& tests) {
    tests.push_back({"self_update.truncated_pe_payload_cannot_replace_a_working_updater", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "old updater");
                         Image(source / L"Updater/updater.exe", "new updater");
                         auto bytes = Read(source / L"Updater/updater.exe");
                         IMAGE_DOS_HEADER dos{};
                         std::memcpy(&dos, bytes.data(), sizeof(dos));
                         bytes.resize(static_cast<size_t>(dos.e_lfanew) + sizeof(IMAGE_NT_HEADERS64));
                         Write(source / L"Updater/updater.exe", bytes);
                         const auto original = Read(target / L"Updater/updater.exe");
                         UpdaterReplacement replacement;
                         CHECK(!replacement.Install(source, target));
                         CHECK(Read(target / L"Updater/updater.exe") == original);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.workflow_rejects_invalid_updater_before_copying_app_files", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "old updater");
                         Image(target / L"d3dxSkinManage-Plus.exe", "old host");
                         Image(source / L"d3dxSkinManage-Plus.exe", "new host");
                         Write(source / L"Updater/updater.exe", "invalid updater");
                         const auto original = Read(target / L"d3dxSkinManage-Plus.exe");
                         bool beganCopy = false;
                         UpdateCallbacks callbacks;
                         callbacks.progress = [&](double value) {
                             if (value > 0) {
                                 beganCopy = true;
                                 throw std::runtime_error("Stop fixture before launch");
                             }
                         };
                         UpdateRequest request;
                         request.pid = MAXDWORD;
                         request.extractDir = source;
                         request.installDir = target;
                         CHECK(!RunUpdate(request, callbacks));
                         CHECK(!beganCopy);
                         CHECK(Read(target / L"d3dxSkinManage-Plus.exe") == original);
                     }});
    tests.push_back({"self_update.workflow_restores_updater_when_app_sync_is_interrupted", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "old updater");
                         Image(source / L"Updater/updater.exe", "new updater");
                         Write(source / L"d3dxSkinManage-Plus.exe", "fixture host");
                         const auto oldBytes = Read(target / L"Updater/updater.exe");
                         const auto newBytes = Read(source / L"Updater/updater.exe");
                         bool sawNewUpdater = false;
                         UpdateCallbacks callbacks;
                         callbacks.progress = [&](double value) {
                             if (value > 0) {
                                 sawNewUpdater = Read(target / L"Updater/updater.exe") == newBytes;
                                 throw std::runtime_error("Injected app sync failure");
                             }
                         };
                         UpdateRequest request;
                         request.pid = MAXDWORD;
                         request.extractDir = source;
                         request.installDir = target;
                         CHECK(!RunUpdate(request, callbacks));
                         CHECK(sawNewUpdater);
                         CHECK(Read(target / L"Updater/updater.exe") == oldBytes);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.commits_updater_and_manifest_before_launch_stage", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "old updater");
                         Image(source / L"Updater/updater.exe", "new updater");
                         Write(source / L"d3dxSkinManage-Plus.exe", "fixture host");
                         Write(source / L"files.manifest", "[managed]\nd3dxSkinManage-Plus.exe\n");
                         const auto newBytes = Read(source / L"Updater/updater.exe");
                         bool ready = false;
                         UpdateCallbacks callbacks;
                         callbacks.step = [&](int number) {
                             if (number == 3) {
                                 ready = fs::exists(target / L"Updater/installed.manifest") &&
                                         Read(target / L"Updater/updater.exe") == newBytes;
                                 throw std::runtime_error("Stop fixture at launch boundary");
                             }
                         };
                         UpdateRequest request;
                         request.pid = MAXDWORD;
                         request.extractDir = source;
                         request.installDir = target;
                         CHECK(!RunUpdate(request, callbacks));
                         CHECK(ready);
                         CHECK(Read(target / L"Updater/updater.exe") == newBytes);
                         CHECK(Retired(target).size() == 1);
                     }});
    tests.push_back({"self_update.replaces_every_package_including_identical_binary", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         const auto current = target / L"Updater/updater.exe";
                         Image(current, "same-version");
                         Image(source / L"Updater/updater.exe", "same-version");
                         const auto bytes = Read(current);
                         UpdaterReplacement replacement;
                         CHECK(replacement.Install(source, target));
                         replacement.Commit();
                         CHECK(Read(current) == bytes);
                         CHECK(Retired(target).size() == 1);
                         CHECK(Read(Retired(target)[0]) == bytes);
                     }});
    tests.push_back({"self_update.rollback_restores_the_original_updater_without_app_backup", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         const auto current = target / L"Updater/updater.exe";
                         Image(current, "old-version");
                         Image(source / L"Updater/updater.exe", "new-version");
                         const auto original = Read(current);
                         {
                             UpdaterReplacement replacement;
                             CHECK(replacement.Install(source, target));
                             CHECK(Read(current) == Read(source / L"Updater/updater.exe"));
                         }
                         CHECK(Read(current) == original);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.invalid_payload_leaves_working_updater_in_place", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "working");
                         Write(source / L"Updater/updater.exe", "not an executable");
                         const auto original = Read(target / L"Updater/updater.exe");
                         UpdaterReplacement replacement;
                         CHECK(!replacement.Install(source, target));
                         CHECK(Read(target / L"Updater/updater.exe") == original);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.missing_payload_keeps_current_updater", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         fs::create_directory(source);
                         Image(target / L"Updater/updater.exe", "working");
                         const auto original = Read(target / L"Updater/updater.exe");
                         UpdaterReplacement replacement;
                         CHECK(replacement.Install(source, target));
                         replacement.Commit();
                         CHECK(Read(target / L"Updater/updater.exe") == original);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.locked_destination_fails_without_removing_the_original", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Image(target / L"Updater/updater.exe", "old");
                         Image(source / L"Updater/updater.exe", "new");
                         const auto original = Read(target / L"Updater/updater.exe");
                         HANDLE locked =
                             CreateFileW((target / L"Updater/updater.exe").c_str(), GENERIC_READ, FILE_SHARE_READ,
                                         nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
                         CHECK(locked != INVALID_HANDLE_VALUE);
                         UpdaterReplacement replacement;
                         const bool installed = replacement.Install(source, target);
                         CloseHandle(locked);
                         CHECK(!installed);
                         CHECK(Read(target / L"Updater/updater.exe") == original);
                         CHECK(Retired(target).empty());
                     }});
    tests.push_back({"self_update.cleanup_only_removes_owned_retired_executables", [] {
                         TempDir temp;
                         Image(temp.path() / L"Updater/updater.exe", "current");
                         Image(temp.path() / L"Updater/updater.exe.old_update.123.1000.1", "retired");
                         Image(temp.path() / L"Updater/updater.exe.old_update", "legacy");
                         Write(temp.path() / L"Updater/notes.old_update", "keep");
                         Write(temp.path() / L"Updater/updater.exe.old_update.not-owned", "keep");
                         Write(temp.path() / L"Updater/Cache/updater.exe.old_update.123.1000.1", "keep");
                         CleanupRetiredUpdaters(temp.path());
                         CHECK(Retired(temp.path()).size() == 2); // 保留不符合当前命名规则的文件。
                         CHECK(fs::is_regular_file(temp.path() / L"Updater/updater.exe.old_update"));
                         CHECK(Read(temp.path() / L"Updater/notes.old_update") == "keep");
                         CHECK(Read(temp.path() / L"Updater/Cache/updater.exe.old_update.123.1000.1") == "keep");
                         CHECK(fs::is_regular_file(temp.path() / L"Updater/updater.exe"));
                     }});
    tests.push_back({"self_update.cleanup_keeps_recovery_images_when_canonical_is_missing", [] {
                         TempDir temp;
                         Image(temp.path() / L"Updater/updater.exe.old_update.123.1000.1", "recovery");
                         CleanupRetiredUpdaters(temp.path());
                         CHECK(Retired(temp.path()).size() == 1);
                     }});
}
}
