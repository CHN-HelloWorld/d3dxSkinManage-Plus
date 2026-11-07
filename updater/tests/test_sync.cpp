#include "sync.h"
#include "test_support.h"

#include <tuple>

namespace tests {
using namespace updater;

class LockedFile {
  public:
    explicit LockedFile(const fs::path& path) {
        handle_ = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
        CHECK(handle_ != INVALID_HANDLE_VALUE);
    }
    ~LockedFile() {
        if (handle_ != INVALID_HANDLE_VALUE)
            CloseHandle(handle_);
    }
    LockedFile(const LockedFile&) = delete;
    LockedFile& operator=(const LockedFile&) = delete;

  private:
    HANDLE handle_ = INVALID_HANDLE_VALUE;
};

void AddSyncTests(TestList& tests) {
    tests.push_back({"sync.plan_copies_actual_files_and_only_removes_old_managed_files", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"files.manifest", "[managed]\nd3dxSkinManage-Plus.exe\n");
                         Write(source / L"d3dxSkinManage-Plus.exe", "new");
                         Write(source / L"Core/new.dll", "new library");
                         Write(source / L"config/user.json", "package defaults");
                         fs::create_directories(source / L"empty/nested");
                         Write(target / L"Core/old.dll", "old");
                         Write(target / L"Core/user.txt", "user data");
                         Write(target / L"config/user.json", "custom");
                         Write(target / L"config/local-only.json", "local only");
                         Write(target / L"d3dxSkinManage-Plus.exe", "old");
                         ManifestSet m;
                         m.installed = {L"d3dxSkinManage-Plus.exe", L"Core/old.dll", L"config/user.json",
                                        L"config/local-only.json"};
                         m.preserve = {L"config/"};
                         const auto plan = CollectSyncPlan(m, source, target);
                         CHECK(plan.toCopy == std::vector<std::wstring>({L"Core/new.dll", L"d3dxSkinManage-Plus.exe"}));
                         CHECK(plan.toRemove == std::vector<std::wstring>({L"Core/old.dll"}));
                         CHECK(plan.sourceDirs ==
                               std::vector<std::wstring>({L"Core", L"config", L"empty", L"empty/nested"}));
                     }});
    tests.push_back({"sync.manifest_updates_official_plugins_and_preserves_unregistered_files", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"files.manifest",
                               "[managed]\nplugins/official.added/entry.py\nplugins/official.test/entry.py\n"
                               "[preserve]\nplugins/\n");
                         Write(target / L"files.manifest",
                               "[managed]\nplugins/official.test/entry.py\nplugins/official.test/old/nested.py\n"
                               "plugins/official.removed/entry.py\n[preserve]\nplugins/\n");
                         Write(source / L"plugins/official.added/entry.py", "added");
                         Write(source / L"plugins/official.test/entry.py", "new");
                         Write(source / L"plugins/official.test/private.json", "package private");
                         Write(source / L"plugins/third.party/entry.py", "package third party");
                         Write(target / L"plugins/official.test/entry.py", "old");
                         Write(target / L"plugins/official.test/old/nested.py", "obsolete");
                         Write(target / L"plugins/official.removed/entry.py", "removed plugin");
                         Write(target / L"plugins/official.test/private.json", "user private");
                         Write(target / L"plugins/official.test/local/entry.py", "local file");
                         Write(target / L"plugins/third.party/entry.py", "user third party");
                         const auto manifests = LoadManifests(source, target, {L"plugins/"});
                         const auto plan = CollectSyncPlan(manifests, source, target);
                         CHECK(plan.toCopy == std::vector<std::wstring>({L"plugins/official.added/entry.py",
                                                                         L"plugins/official.test/entry.py"}));
                         CHECK(plan.toRemove == std::vector<std::wstring>({L"plugins/official.removed/entry.py",
                                                                           L"plugins/official.test/old/nested.py"}));
                         CHECK(SyncFiles(manifests, source, target, plan));
                         CHECK(Read(target / L"plugins/official.added/entry.py") == "added");
                         CHECK(Read(target / L"plugins/official.test/entry.py") == "new");
                         CHECK(!fs::exists(target / L"plugins/official.test/old"));
                         CHECK(!fs::exists(target / L"plugins/official.removed"));
                         CHECK(Read(target / L"plugins/official.test/private.json") == "user private");
                         CHECK(Read(target / L"plugins/official.test/local/entry.py") == "local file");
                         CHECK(Read(target / L"plugins/third.party/entry.py") == "user third party");
                     }});
    tests.push_back({"sync.plugin_preserve_exception_requires_explicit_file_entries", [] {
                         ManifestSet manifests;
                         manifests.managed = {L"plugins/official.test/entry.py", L"data/private.json"};
                         manifests.installed = {L"plugins/official.old/entry.py"};
                         manifests.preserve = {L"plugins/"};
                         CHECK(!IsPreserved(manifests, L"plugins"));
                         CHECK(!IsPreserved(manifests, L"plugins/official.test"));
                         CHECK(!IsPreserved(manifests, L"PLUGINS\\OFFICIAL.TEST\\ENTRY.PY"));
                         CHECK(!IsPreserved(manifests, L"plugins/official.old/entry.py"));
                         CHECK(IsPreserved(manifests, L"plugins/official.test/private.json"));
                         CHECK(IsPreserved(manifests, L"plugins/official.test/local/entry.py"));
                         CHECK(IsPreserved(manifests, L"plugins/official.test-extra/entry.py"));
                         CHECK(IsPreserved(manifests, L"plugins/third.party/entry.py"));
                         CHECK(IsPreserved(manifests, L"data/private.json"));
                         manifests.managed = {L"plugins/", L"plugins/official.test/",
                                              L"plugins/official.test/../third.party/entry.py"};
                         manifests.installed.clear();
                         CHECK(IsPreserved(manifests, L"plugins/official.test/private.json"));
                         CHECK(IsPreserved(manifests, L"plugins/third.party/entry.py"));
                     }});
    tests.push_back({"sync.no_baseline_never_deletes_existing_files", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/d3dxSkinManage-Plus.exe", "new");
                         Write(temp.path() / L"installed/old.dll", "keep");
                         ManifestSet m;
                         m.managed = {L"old.dll"};
                         CHECK(CollectSyncPlan(m, temp.path() / L"new", temp.path() / L"installed").toRemove.empty());
                     }});
    tests.push_back({"sync.applies_plan_preserves_users_and_retains_new_empty_directories", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"d3dxSkinManage-Plus.exe", "new program");
                         Write(source / L"files.manifest", "[managed]\nd3dxSkinManage-Plus.exe\n[preserve]\nconfig/\n");
                         Write(source / L"config/settings.json", "defaults");
                         fs::create_directories(source / L"empty/nested");
                         Write(target / L"d3dxSkinManage-Plus.exe", "old program");
                         Write(target / L"Old/nested/obsolete.dll", "old");
                         Write(target / L"config/settings.json", "user configuration");
                         Write(target / L"config/local-only.json", "local only");
                         Write(target / L"GameAssets/mod.ini", "user mod");
                         Write(target / L"files.manifest", "root baseline stays");
                         fs::create_directory(target / L"user-empty");
                         ManifestSet m;
                         m.installed = {L"d3dxSkinManage-Plus.exe", L"Old/nested/obsolete.dll", L"config/settings.json",
                                        L"config/local-only.json"};
                         m.preserve = {L"config/"};
                         const auto plan = CollectSyncPlan(m, source, target);
                         std::vector<std::pair<size_t, size_t>> progress;
                         CHECK(
                             SyncFiles(m, source, target, plan, [&](size_t current, size_t total, const std::wstring&) {
                                 progress.emplace_back(current, total);
                             }));
                         CHECK(Read(target / L"d3dxSkinManage-Plus.exe") == "new program");
                         CHECK(fs::last_write_time(target / L"d3dxSkinManage-Plus.exe") ==
                               fs::last_write_time(source / L"d3dxSkinManage-Plus.exe"));
                         CHECK(Read(target / L"config/settings.json") == "user configuration");
                         CHECK(Read(target / L"config/local-only.json") == "local only");
                         CHECK(Read(target / L"GameAssets/mod.ini") == "user mod");
                         CHECK(Read(target / L"files.manifest") == "root baseline stays");
                         CHECK(!fs::exists(target / L"Old"));
                         CHECK(fs::is_directory(target / L"empty/nested"));
                         CHECK(fs::is_directory(target / L"user-empty"));
                         CHECK(progress == std::vector<std::pair<size_t, size_t>>({{1, 2}, {2, 2}}));
                     }});
    tests.push_back({"sync.locked_copy_failure_is_reported_without_destroying_old_data", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"locked.dll", "new");
                         Write(target / L"locked.dll", "original");
                         LockedFile lock(target / L"locked.dll");
                         SyncPlan plan;
                         plan.toCopy = {L"locked.dll"};
                         CHECK(!SyncFiles({}, source, target, plan));
                         CHECK(Read(target / L"locked.dll") == "original");
                     }});
    tests.push_back({"sync.old_file_deletion_failure_is_nonfatal", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         Write(target / L"locked.dll", "old");
                         LockedFile lock(target / L"locked.dll");
                         SyncPlan plan;
                         plan.toRemove = {L"locked.dll"};
                         ManifestSet m;
                         m.installed = {L"locked.dll"};
                         fs::create_directory(temp.path() / L"new");
                         CHECK(SyncFiles(m, temp.path() / L"new", target, plan));
                         CHECK(Read(target / L"locked.dll") == "old");
                     }});
    tests.push_back({"sync.rollback_replaces_backed_up_items_and_keeps_other_user_data", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto backup = target / L"Updater/Cache/backup";
                         Write(backup / L"d3dxSkinManage-Plus.exe", "old program");
                         Write(backup / L"Core/old.dll", "old library");
                         Write(target / L"d3dxSkinManage-Plus.exe", "partial new program");
                         Write(target / L"Core/new.dll", "partial new library");
                         Write(target / L"config/user.json", "user");
                         SyncPlan plan;
                         plan.newFiles = {L"Core/new.dll"};
                         CHECK(RollbackFromBackup(backup, target, {}, &plan));
                         CHECK(Read(target / L"d3dxSkinManage-Plus.exe") == "old program");
                         CHECK(Read(target / L"Core/old.dll") == "old library");
                         CHECK(!fs::exists(target / L"Core/new.dll"));
                         CHECK(Read(target / L"config/user.json") == "user");
                         CHECK(fs::exists(backup / L"d3dxSkinManage-Plus.exe"));
                     }});
    tests.push_back({"sync.saves_baseline_before_extract_cleanup_without_changing_root_manifest", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto source = temp.path() / L"extract";
                         const std::string manifest = "[managed]\nd3dxSkinManage-Plus.exe\n[preserve]\nconfig/\n";
                         Write(source / L"files.manifest", manifest);
                         Write(target / L"files.manifest", "root baseline");
                         SaveInstalledManifest(target, source);
                         CleanupExtract(source);
                         CHECK(Read(target / L"Updater/installed.manifest") == manifest);
                         CHECK(Read(target / L"files.manifest") == "root baseline");
                         CHECK(!fs::exists(source));
                         SaveInstalledManifest(target, source);
                         CHECK(Read(target / L"Updater/installed.manifest") == manifest);
                     }});
    tests.push_back(
        {"sync.removes_readonly_backup_and_stale_rename_files", [] {
             TempDir temp;
             const auto backup = temp.path() / L"backup";
             Write(backup / L".git/objects/readonly", "readonly");
             CHECK(SetFileAttributesW((backup / L".git/objects/readonly").c_str(), FILE_ATTRIBUTE_READONLY) != FALSE);
             CHECK(RobustRemoveAll(backup));
             CHECK(!fs::exists(backup));
             Write(temp.path() / L"Core/old.dll.old_update", "stale");
             Write(temp.path() / L"keep.dll", "keep");
             CleanupOldUpdateFiles(temp.path());
             CHECK(!fs::exists(temp.path() / L"Core/old.dll.old_update"));
             CHECK(Read(temp.path() / L"keep.dll") == "keep");
         }});
}

}
