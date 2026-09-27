#include "logger.h"
#include "manifest.h"
#include "test_support.h"
#include "token.h"
#include "util.h"

#include <algorithm>
#include <chrono>
#include <regex>
#include <thread>

namespace tests {
using namespace updater;

void AddCoreTests(TestList& tests) {
    tests.push_back({"unicode.preserves_chinese_emoji_and_embedded_nul", [] {
                         const std::string utf8 = u8"配置/🚀/文件.txt";
                         const std::wstring wide = L"配置/🚀/文件.txt";
                         CHECK(Utf8ToWide(utf8) == wide);
                         CHECK(WideToUtf8(wide) == utf8);
                         CHECK(Utf8ToWide(std::string("a\0b", 3)) == std::wstring(L"a\0b", 3));
                         CHECK(WideToUtf8(std::wstring(L"a\0b", 3)) == std::string("a\0b", 3));
                         CHECK(Utf8ToWide("").empty());
                         CHECK(WideToUtf8(L"").empty());
                         CHECK(NormalizeRel(L"Core\\模块.dll") == L"Core/模块.dll");
                     }});
    tests.push_back(
        {"unicode.rejects_invalid_utf8", [] { CheckThrows([] { Utf8ToWide(std::string("\xc3\x28", 2)); }); }});
    tests.push_back(
        {"json.decodes_launcher_escaping_and_unicode", [] {
             const auto actual =
                 ParseJsonStringArray(LR"(["config/", "\u914d\u7f6e\/", "quote\"slash\\\n\t\b\f\r", "\ud83d\ude80"])");
             const std::vector<std::wstring> expected{L"config/", L"配置/", L"quote\"slash\\\n\t\b\f\r", L"🚀"};
             CHECK(actual == expected);
             CHECK(ParseJsonStringArray(L" \r\n [ ] \t").empty());
             CHECK(ParseJsonStringArray(LR"(["", "路径/"])") == std::vector<std::wstring>({L"", L"路径/"}));
         }});
    tests.push_back({"json.rejects_malformed_or_non_string_arrays", [] {
                         const std::vector<std::wstring> bad{L"",
                                                             L"null",
                                                             L"{}",
                                                             L"[1]",
                                                             L"[true]",
                                                             L"[[]]",
                                                             L"[\"a\",]",
                                                             L"[\"a\" \"b\"]",
                                                             L"[\"a\"] trailing",
                                                             L"[\"unterminated]",
                                                             LR"(["\x41"])",
                                                             LR"(["\uXYZ0"])",
                                                             L"[\"raw\nnewline\"]"};
                         for (const auto& json : bad)
                             CheckThrows([&] { ParseJsonStringArray(json); });
                     }});
    tests.push_back({"token.accepts_python_hmac_and_inclusive_120_second_boundary", [] {
                         constexpr std::int64_t now = 1700000000;
                         CHECK(VerifyLaunchToken(
                             L"6553f100.32fc18b59b276aa33dd589c197e90fe49859e6d696febfc381f201caa192d019", now));
                         CHECK(VerifyLaunchToken(
                             L"6553f088.7409112b943520aaa7132e64232946da21f43ebde4b9a5c6892832544151a9f4", now));
                         CHECK(VerifyLaunchToken(
                             L"6553f178.0bc7f201ea78d7c50fdc7fc71085bf5388bd5738cfbd5ba56b12ee88608ac935", now));
                     }});
    tests.push_back({"token.rejects_stale_future_tampered_and_malformed_tokens", [] {
                         constexpr std::int64_t now = 1700000000;
                         const std::vector<std::wstring> bad{
                             L"6553f087.f24248e27167fb1e0a1c47253cbea1e59f0fd9baee66c6cbb22936e9a2def4f5",
                             L"6553f179.16768744b2c64cf5101cd765e88031d1e0072e81d92fdbaf0b152d5a88fe01a2",
                             L"6553f100.d31d895e887d681a66bc5f0f00945715e143b723190df02f0ce6e6bdbe9a2310",
                             L"6553f100.32FC18B59B276AA33DD589C197E90FE49859E6D696FEBFC381F201CAA192D019",
                             L"6553f100",
                             L".abc",
                             L"garbage.abc",
                             L"ffffffffff.abc",
                             L"1.a.b",
                             L"",
                             L"6553f100.",
                             L"6553f100.32fc18b59b276aa33dd589c197e90fe49859e6d696febfc381f201caa192d019.extra"};
                         for (const auto& token : bad)
                             CHECK(!VerifyLaunchToken(token, now));
                     }});
    tests.push_back({"manifest.prefers_installed_baseline_and_new_preserve", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         Write(source / L"files.manifest",
                               "[managed]\r\nd3dxSkinManage-Plus.exe\r\nCore/new.dll\n\n[preserve]\nconfig/\nlog/\n");
                         Write(target / L"files.manifest", "[managed]\nfallback-only.dll\n");
                         Write(target / L"Updater/installed.manifest",
                               "[managed]\nCore/old.dll\n[preserve]\nold-data/\n");
                         const auto m = LoadManifests(source, target, {L"fallback/"});
                         CHECK(m.managed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe", L"Core/new.dll"}));
                         CHECK(m.installed == std::vector<std::wstring>({L"Core/old.dll"}));
                         CHECK(IsPreserved(m, L"config/user.json"));
                         CHECK(IsPreserved(m, L"log/current.log"));
                         CHECK(IsPreserved(m, L"fallback/user.dat"));
                     }});
    tests.push_back(
        {"manifest.falls_back_to_root_manifest", [] {
             TempDir temp;
             Write(temp.path() / L"new/files.manifest", "[managed]\n d3dxSkinManage-Plus.exe \n\nCore/new.dll\n");
             Write(temp.path() / L"installed/files.manifest", "[managed]\nd3dxSkinManage-Plus.exe\nCore/old.dll\n");
             const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {L"config/"});
             CHECK(m.managed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe", L"Core/new.dll"}));
             CHECK(m.installed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe", L"Core/old.dll"}));
             CHECK(m.preserve == std::vector<std::wstring>({L"config/"}));
         }});
    tests.push_back({"manifest.headerless_input_cannot_authorize_deletion", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/files.manifest", "d3dxSkinManage-Plus.exe\n");
                         Write(temp.path() / L"installed/files.manifest", "[managed]\nCore/old.dll\n");
                         const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {L"config/"});
                         CHECK(m.managed.empty());
                         CHECK(m.installed.empty());
                         CHECK(!IsInManifest(m, L"Core/old.dll"));
                     }});
    tests.push_back({"manifest.missing_new_manifest_disables_deletion_even_with_baseline", [] {
                         TempDir temp;
                         fs::create_directory(temp.path() / L"new");
                         Write(temp.path() / L"installed/Updater/installed.manifest", "[managed]\nCore/old.dll\n");
                         const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {L"config/"});
                         CHECK(m.managed.empty());
                         CHECK(m.installed.empty());
                         CHECK(m.preserve == std::vector<std::wstring>({L"config/"}));
                         CHECK(!IsInManifest(m, L"Core/old.dll"));
                     }});
    tests.push_back({"manifest.handles_cr_only_and_unicode_line_separators", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/files.manifest",
                               "[managed]\rd3dxSkinManage-Plus.exe\r[preserve]\rsaves/\r");
                         Write(temp.path() / L"installed/files.manifest",
                               u8"[managed]\u0085Core/old.dll\u2028Core/older.dll\u2029");
                         const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {L"config/"});
                         CHECK(m.managed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe"}));
                         CHECK(IsPreserved(m, L"saves/slot.dat"));
                         CHECK(IsPreserved(m, L"config/settings.json"));
                         CHECK(m.installed == std::vector<std::wstring>({L"Core/old.dll", L"Core/older.dll"}));
                     }});
    tests.push_back({"manifest.ignores_unknown_sections_and_retains_argv_fallback", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/files.manifest",
                               "[managed]\nd3dxSkinManage-Plus.exe\n[metadata]\nignore\n[preserve]\n\n");
                         Write(temp.path() / L"installed/files.manifest", "[managed]\nCore/old.dll\n[other]\nignore\n");
                         const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {L"log/"});
                         CHECK(m.managed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe"}));
                         CHECK(m.installed == std::vector<std::wstring>({L"Core/old.dll"}));
                         CHECK(m.preserve == std::vector<std::wstring>({L"log/"}));
                     }});
    tests.push_back({"manifest.no_baseline_keeps_new_configuration_without_deletion", [] {
                         TempDir temp;
                         Write(temp.path() / L"new/files.manifest",
                               "[managed]\nd3dxSkinManage-Plus.exe\n[preserve]\nlog/\n");
                         fs::create_directory(temp.path() / L"installed");
                         const auto m = LoadManifests(temp.path() / L"new", temp.path() / L"installed", {});
                         CHECK(m.managed == std::vector<std::wstring>({L"d3dxSkinManage-Plus.exe"}));
                         CHECK(m.preserve == std::vector<std::wstring>({L"log/"}));
                         CHECK(m.installed.empty());
                     }});
    tests.push_back({"manifest.preserve_matches_directories_and_exact_files_case_insensitively", [] {
                         ManifestSet m;
                         m.preserve = {L"config/", L"log/", L"keep.ini"};
                         for (const auto* path : {L"config", L"config/", L"Config/a", L"config/nested/user.json",
                                                  L"log/today.log", L"keep.ini"}) {
                             CHECK(IsPreserved(m, path));
                         }
                         for (const auto* path : {L"config2/a", L"keep.ini.bak", L"keep.ini/child", L"Core/app.dll"}) {
                             CHECK(!IsPreserved(m, path));
                         }
                     }});
    tests.push_back({"manifest.managed_queries_only_match_installed_entries", [] {
                         ManifestSet m;
                         m.managed = {L"new.dll"};
                         CHECK(!IsInManifest(m, L"new.dll"));
                         m.installed = {L"Core/old.dll", L"legacy/"};
                         for (const auto* path : {L"Core/old.dll", L"core/old.dll", L"Core\\old.dll", L"Core/",
                                                  L"legacy", L"legacy/a.txt"}) {
                             CHECK(IsInManifest(m, path));
                         }
                         for (const auto* path : {L"new.dll", L"Core", L"Core/user.txt", L"GameAssets/user.ini"}) {
                             CHECK(!IsInManifest(m, path));
                         }
                     }});
    tests.push_back(
        {"logger.preserves_previous_logs_and_writes_utf8", [] {
             TempDir temp;
             const auto logRoot = temp.path() / L"logs";
             const auto logDir = logRoot / L"updater";
             const auto previous = logDir / L"20000101_000000.log";
             Write(previous, "previous");
             Write(logRoot / L"application.log", "untouched");
             InitLog(temp.path());
             LogInfo(L"中文日志 🚀");
             CloseLog();
             CHECK(Read(previous) == "previous");
             CHECK(Read(logRoot / L"application.log") == "untouched");
             CHECK(fs::is_directory(logDir));
             size_t written = 0;
             for (const auto& entry : fs::directory_iterator(logDir)) {
                 if (entry.path() == previous)
                     continue;
                 CHECK(std::regex_match(entry.path().filename().wstring(), std::wregex(L"[0-9]{8}_[0-9]{6}\\.log")));
                 const auto data = Read(entry.path());
                 CHECK(data.find(u8"[INFO] 中文日志 🚀") != std::string::npos);
                 CHECK(data.find(u8"结束时间:") != std::string::npos);
                 CHECK(data.substr(0, 3) != "\xef\xbb\xbf");
                 ++written;
             }
             CHECK(written == 1);
         }});
    tests.push_back({"logger.concurrent_messages_are_complete", [] {
                         TempDir temp;
                         InitLog(temp.path());
                         std::vector<std::thread> writers;
                         for (int n = 0; n < 3; ++n) {
                             writers.emplace_back([n] {
                                 for (int i = 0; i < 25; ++i)
                                     LogInfo(L"worker-" + std::to_wstring(n) + L"-" + std::to_wstring(i));
                             });
                         }
                         for (auto& writer : writers)
                             writer.join();
                         CloseLog();
                         size_t messages = 0;
                         const auto dir = temp.path() / L"logs/updater";
                         CHECK(fs::is_directory(dir));
                         for (const auto& entry : fs::directory_iterator(dir)) {
                             const auto data = Read(entry.path());
                             size_t pos = 0;
                             while ((pos = data.find("[INFO] worker-", pos)) != std::string::npos) {
                                 ++messages;
                                 ++pos;
                             }
                         }
                         CHECK(messages == 75);
                     }});
    tests.push_back({"logger.initialization_failure_does_not_abort_update", [] {
                         TempDir temp;
                         Write(temp.path() / L"logs", "a file blocks the log directory");
                         InitLog(temp.path());
                         LogInfo(L"still updating");
                         CloseLog();
                         CHECK(Read(temp.path() / L"logs") == "a file blocks the log directory");
                     }});
}

}
