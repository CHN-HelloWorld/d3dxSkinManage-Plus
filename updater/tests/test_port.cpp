#include "logger.h"
#include "manifest.h"
#include "sync.h"
#include "test_support.h"
#include "token.h"

namespace tests {
using namespace updater;

void AddPortTests(TestList& tests) {
    tests.push_back(
        {"port.token_matches_host_hmac_and_inclusive_120_second_boundary", [] {
             constexpr std::int64_t now = 1700000000;
             CHECK(
                 VerifyLaunchToken(L"6553f100.32fc18b59b276aa33dd589c197e90fe49859e6d696febfc381f201caa192d019", now));
             CHECK(
                 VerifyLaunchToken(L"6553f088.7409112b943520aaa7132e64232946da21f43ebde4b9a5c6892832544151a9f4", now));
             CHECK(
                 VerifyLaunchToken(L"6553f178.0bc7f201ea78d7c50fdc7fc71085bf5388bd5738cfbd5ba56b12ee88608ac935", now));
             CHECK(
                 !VerifyLaunchToken(L"6553f087.f24248e27167fb1e0a1c47253cbea1e59f0fd9baee66c6cbb22936e9a2def4f5", now));
             CHECK(
                 !VerifyLaunchToken(L"6553f179.16768744b2c64cf5101cd765e88031d1e0072e81d92fdbaf0b152d5a88fe01a2", now));
         }});
    tests.push_back({"port.preserved_windows_paths_protect_user_data", [] {
                         ManifestSet manifests;
                         for (const auto* path : {L"data/mod.zip", L"Config/settings.json", L"cache/a", L"LOGS/a.log",
                                                  L"plugins/custom/entry.py", L"Updater/Cache/backup/app.exe",
                                                  L"CONFIG\\LANGUAGES\\en-US\\strings.json"}) {
                             CHECK(IsPreserved(manifests, path));
                         }
                         CHECK(!IsPreserved(manifests, L"config-extra/settings.json"));
                     }});
    tests.push_back({"port.directory_rules_do_not_delete_sibling_prefixes", [] {
                         ManifestSet manifests;
                         manifests.installed = {L"legacy/"};
                         CHECK(IsInManifest(manifests, L"LEGACY/old.dll"));
                         CHECK(!IsInManifest(manifests, L"legacy-user/save.dat"));
                         CHECK(!IsInManifest(manifests, L"legacy2/custom.ini"));
                     }});
    tests.push_back({"port.invalid_new_manifest_disables_old_file_deletion", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/files.manifest", std::string("\xc3\x28", 2));
                         Write(temp.path() / L"installed/Updater/installed.manifest", "[managed]\nold.dll\n");
                         const auto manifests = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {});
                         CHECK(!IsInManifest(manifests, L"old.dll"));
                     }});
    tests.push_back({"language.sync_adds_missing_pack_without_overwriting_custom_config", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"config/languages/en-US/strings.json", "packaged English");
                         Write(source / L"config/languages/ja-JP/strings.json", "packaged Japanese");
                         Write(source / L"config/settings.json", "defaults");
                         Write(target / L"config/languages/en-US/strings.json", "custom English");
                         Write(target / L"config/settings.json", "custom settings");
                         const auto plan = CollectSyncPlan({}, source, target);
                         CHECK(SyncFiles({}, source, target, plan));
                         CHECK(Read(target / L"config/languages/en-US/strings.json") == "custom English");
                         CHECK(Read(target / L"config/settings.json") == "custom settings");
                         CHECK(Read(target / L"config/languages/ja-JP/strings.json") == "packaged Japanese");
                     }});
    tests.push_back({"language.sync_respects_config_created_after_planning", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"config/languages/en-US/strings.json", "defaults");
                         fs::create_directory(target);
                         const auto plan = CollectSyncPlan({}, source, target);
                         Write(target / L"config/languages/en-US/strings.json", "created after planning");
                         CHECK(SyncFiles({}, source, target, plan));
                         CHECK(Read(target / L"config/languages/en-US/strings.json") == "created after planning");
                     }});
    tests.push_back({"port.failed_copy_does_not_remove_obsolete_files", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         fs::create_directory(source);
                         Write(target / L"old.dll", "needed by old version");
                         SyncPlan plan;
                         plan.toCopy = {L"missing.dll"};
                         plan.toRemove = {L"old.dll"};
                         ManifestSet manifests;
                         manifests.installed = {L"old.dll"};
                         CHECK(!SyncFiles(manifests, source, target, plan));
                         CHECK(Read(target / L"old.dll") == "needed by old version");
                     }});
    tests.push_back({"port.plan_rejects_overlapping_roots", [] {
                         TempDir temp;
                         Write(temp.path() / L"app.exe", "program");
                         CheckThrows([&] { CollectSyncPlan({}, temp.path(), temp.path()); });
                         fs::create_directory(temp.path() / L"child");
                         CheckThrows([&] { CollectSyncPlan({}, temp.path(), temp.path() / L"child"); });
                     }});
    tests.push_back({"port.stale_cleanup_does_not_touch_user_directories", [] {
                         TempDir temp;
                         Write(temp.path() / L"data/custom.old_update", "user file");
                         Write(temp.path() / L"Updater/Cache/backup/old.old_update", "recovery file");
                         Write(temp.path() / L"Core/old.dll.old_update", "stale program");
                         CleanupOldUpdateFiles(temp.path());
                         CHECK(Read(temp.path() / L"data/custom.old_update") == "user file");
                         CHECK(Read(temp.path() / L"Updater/Cache/backup/old.old_update") == "recovery file");
                         CHECK(!fs::exists(temp.path() / L"Core/old.dll.old_update"));
                     }});
}

}
