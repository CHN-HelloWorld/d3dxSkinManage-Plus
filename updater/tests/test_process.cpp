#include "proc.h"
#include "self_update.h"
#include "sync.h"
#include "test_support.h"
#include "workflow.h"

namespace tests {
using namespace updater;
namespace {

fs::path SelfPath() {
    std::wstring buffer(32768, L'\0');
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    CHECK(length != 0 && length < buffer.size());
    buffer.resize(length);
    return fs::path(buffer);
}

// 仅运行测试程序自身，通过命名事件控制退出。
class ImageProcess {
  public:
    explicit ImageProcess(const fs::path& path, bool copy = true, const fs::path& replacement = {}) {
        if (copy)
            CHECK(CopyFileW(SelfPath().c_str(), path.c_str(), FALSE) != FALSE);
        static std::atomic<unsigned long> serial{0};
        const auto eventName = L"Local\\D3dxUpdaterTest-" + std::to_wstring(GetCurrentProcessId()) + L"-" +
                               std::to_wstring(GetTickCount64()) + L"-" + std::to_wstring(serial++);
        event_ = CreateEventW(nullptr, TRUE, FALSE, eventName.c_str());
        CHECK(event_ != nullptr);
        auto command =
            L"\"" + path.wstring() + L"\" " +
            (replacement.empty() ? L"--hold-image " : L"--replace-own-image \"" + replacement.wstring() + L"\" ") +
            L"\"" + eventName + L"\"";
        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        PROCESS_INFORMATION process{};
        if (!CreateProcessW(path.c_str(), command.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr,
                            path.parent_path().c_str(), &startup, &process)) {
            CloseHandle(event_);
            event_ = nullptr;
            throw std::runtime_error("Could not launch test image fixture");
        }
        process_ = process.hProcess;
        pid_ = process.dwProcessId;
        CloseHandle(process.hThread);
    }
    ~ImageProcess() {
        SetEvent(event_);
        if (WaitForSingleObject(process_, 5000) == WAIT_TIMEOUT) {
            TerminateProcess(process_, 1);
            WaitForSingleObject(process_, 5000);
        }
        CloseHandle(process_);
        CloseHandle(event_);
    }
    ImageProcess(const ImageProcess&) = delete;
    ImageProcess& operator=(const ImageProcess&) = delete;
    DWORD pid() const {
        return pid_;
    }
    bool running() const {
        return WaitForSingleObject(process_, 0) == WAIT_TIMEOUT;
    }
    void Stop() {
        SetEvent(event_);
    }

  private:
    HANDLE event_ = nullptr;
    HANDLE process_ = nullptr;
    DWORD pid_ = 0;
};

// 使用测试程序副本模拟新版主程序，更新材料全部位于独立安装夹具中。
UpdateRequest PrepareUpdate(const fs::path& target) {
    const auto source = target / L"Updater/Cache/extract";
    Write(source / L"files.manifest", "[managed]\nd3dxSkinManage-Plus.exe\nCore/library.dll\n");
    Write(source / L"Core/library.dll", "new library");
    CHECK(CopyFileW(SelfPath().c_str(), (source / L"d3dxSkinManage-Plus.exe").c_str(), FALSE) != FALSE);
    Write(target / L"d3dxSkinManage-Plus.exe", "old program");
    Write(target / L"Core/library.dll", "old library");
    Write(target / L"Updater/Cache/backup/d3dxSkinManage-Plus.exe", "old program");
    Write(target / L"Updater/Cache/backup/Core/library.dll", "old library");
    Write(target / L"Updater/Cache/download/update.zip", "downloaded update");
    UpdateRequest request;
    request.pid = MAXDWORD;
    request.extractDir = source;
    request.installDir = target;
    return request;
}

// 等待模拟主程序写入启动凭据并退出，确保临时安装目录清理前不再有运行中的副本。
void WaitForLaunchedFixture(const fs::path& target) {
    const auto marker = target / L"launched.txt";
    const auto deadline = GetTickCount64() + 5000;
    while (!fs::exists(marker) && GetTickCount64() < deadline)
        Sleep(10);
    CHECK(fs::exists(marker));
    const auto pid = static_cast<DWORD>(std::stoul(Read(marker)));
    CHECK(WaitForProcessExit(pid, 5000));
}

}

bool RunFixtureMode(int argc, wchar_t** argv, int& result) {
    if (argc >= 2 && std::wstring(argv[1]) == L"--replace-own-image") {
        result = 1;
        const auto self = SelfPath();
        if (argc != 4 || self.filename() != L"updater.exe" || self.parent_path().filename() != L"Updater")
            return true;
        UpdaterReplacement replacement;
        const auto install = self.parent_path().parent_path();
        if (!replacement.Install(fs::path(argv[2]), install))
            return true;
        replacement.Commit();
        Write(install / L"self-replaced.txt", "old process continued after replacing itself");
        const HANDLE event = OpenEventW(SYNCHRONIZE, FALSE, argv[3]);
        if (event) {
            result = WaitForSingleObject(event, 10000) == WAIT_OBJECT_0 ? 0 : 1;
            CloseHandle(event);
        }
        return true;
    }
    if (argc >= 2 && std::wstring(argv[1]) == L"--hold-image") {
        result = 1;
        if (argc != 3)
            return true;
        const HANDLE event = OpenEventW(SYNCHRONIZE, FALSE, argv[2]);
        if (event) {
            result = WaitForSingleObject(event, 10000) == WAIT_OBJECT_0 ? 0 : 1;
            CloseHandle(event);
        }
        return true;
    }
    const auto self = SelfPath();
    if (self.filename() == L"d3dxSkinManage-Plus.exe") {
        result = 1;
        // 仅写入测试程序所在的临时目录，不使用调用方工作目录。
        if (fs::equivalent(fs::current_path(), self.parent_path())) {
            Write(self.parent_path() / L"launched.tmp", std::to_string(GetCurrentProcessId()));
            fs::rename(self.parent_path() / L"launched.tmp", self.parent_path() / L"launched.txt");
            result = 0;
        }
        return true;
    }
    return false;
}

void AddProcessTests(TestList& tests) {
    tests.push_back({"self_update.running_image_replaces_itself_and_new_path_launches", [] {
                         TempDir temp;
                         const auto source = temp.path() / L"new";
                         const auto target = temp.path() / L"installed";
                         const auto current = target / L"Updater/updater.exe";
                         fs::create_directories(current.parent_path());
                         const auto newBytes = Read(SelfPath()) + "new updater test payload";
                         Write(source / L"Updater/updater.exe", newBytes);
                         fs::path retired;
                         {
                             ImageProcess old(current, true, source);
                             const auto marker = target / L"self-replaced.txt";
                             const auto deadline = GetTickCount64() + 5000;
                             while (!fs::exists(marker) && old.running() && GetTickCount64() < deadline)
                                 Sleep(10);
                             CHECK(fs::exists(marker));
                             CHECK(old.running());
                             CHECK(Read(current) == newBytes);
                             for (const auto& entry : fs::directory_iterator(current.parent_path())) {
                                 if (entry.path().filename().wstring().find(L"updater.exe.old_update.") == 0)
                                     retired = entry.path();
                             }
                             CHECK(!retired.empty());
                             CleanupRetiredUpdaters(target);
                             CHECK(fs::exists(retired));
                             ImageProcess next(current, false);
                             CHECK(next.running());
                         }
                         CleanupRetiredUpdaters(target);
                         CHECK(!fs::exists(retired));
                         CHECK(Read(current) == newBytes);
                     }});
    tests.push_back({"process.wait_detects_running_and_exited_children", [] {
                         TempDir temp;
                         ImageProcess child(temp.path() / L"wait-fixture.exe");
                         CHECK(!WaitForProcessExit(child.pid(), 1));
                         child.Stop();
                         CHECK(WaitForProcessExit(child.pid(), 5000));
                         CHECK(WaitForProcessExit(MAXDWORD, 1));
                     }});
    tests.push_back(
        {"process.launch_uses_install_directory_and_handles_missing_executable", [] {
             TempDir temp;
             CHECK(!LaunchNewVersion(temp.path()));
             CHECK(CopyFileW(SelfPath().c_str(), (temp.path() / L"d3dxSkinManage-Plus.exe").c_str(), FALSE) != FALSE);
             CHECK(LaunchNewVersion(temp.path()));
             const auto marker = temp.path() / L"launched.txt";
             const auto deadline = GetTickCount64() + 5000;
             while (!fs::exists(marker) && GetTickCount64() < deadline)
                 Sleep(10);
             CHECK(fs::exists(marker));
             const auto pid = static_cast<DWORD>(std::stoul(Read(marker)));
             CHECK(WaitForProcessExit(pid, 5000));
         }});
    tests.push_back({"sync.renames_running_image_then_copies_replacement", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto source = temp.path() / L"new";
                         fs::create_directory(target);
                         Write(source / L"updater.exe", "replacement image");
                         {
                             ImageProcess child(target / L"updater.exe");
                             SyncPlan plan;
                             plan.toCopy = {L"updater.exe"};
                             CHECK(SyncFiles({}, source, target, plan));
                             CHECK(Read(target / L"updater.exe") == "replacement image");
                             CHECK(fs::exists(target / L"updater.exe.old_update"));
                         }
                         CleanupOldUpdateFiles(target);
                         CHECK(!fs::exists(target / L"updater.exe.old_update"));
                     }});
    tests.push_back({"workflow.success_cleans_update_caches_after_launch", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto request = PrepareUpdate(target);
                         Write(target / L"Updater/Cache/download/previous.zip", "older download");
                         Write(target / L"Updater/Cache/download/update.zip.part", "partial download");
                         Write(target / L"Updater/Cache/keep.txt", "other updater cache");
                         Write(target / L"Updater/keep.txt", "updater data");
                         Write(target / L"config/settings.json", "user settings");
                         Write(target / L"cache/keep.zip", "application cache");
                         bool keptUntilLaunch = false;
                         UpdateCallbacks callbacks;
                         callbacks.step = [&](int number) {
                             if (number == 3) {
                                 keptUntilLaunch = fs::exists(request.extractDir) &&
                                                   fs::exists(target / L"Updater/Cache/backup") &&
                                                   fs::exists(target / L"Updater/Cache/download/update.zip");
                             }
                         };
                         CHECK(RunUpdate(request, callbacks));
                         WaitForLaunchedFixture(target);
                         CHECK(keptUntilLaunch);
                         CHECK(!fs::exists(target / L"Updater/Cache/download"));
                         CHECK(!fs::exists(request.extractDir));
                         CHECK(!fs::exists(target / L"Updater/Cache/backup"));
                         CHECK(Read(target / L"Updater/Cache/keep.txt") == "other updater cache");
                         CHECK(Read(target / L"Updater/keep.txt") == "updater data");
                         CHECK(Read(target / L"config/settings.json") == "user settings");
                         CHECK(Read(target / L"cache/keep.zip") == "application cache");
                         CHECK(Read(target / L"Core/library.dll") == "new library");
                         CHECK(fs::exists(target / L"Updater/installed.manifest"));
                     }});
    tests.push_back({"workflow.sync_failure_keeps_update_caches", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto request = PrepareUpdate(target);
                         const HANDLE lock =
                             CreateFileW((target / L"Core/library.dll").c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
                         CHECK(lock != INVALID_HANDLE_VALUE);
                         const bool updated = RunUpdate(request);
                         CloseHandle(lock);
                         CHECK(!updated);
                         CHECK(Read(target / L"Updater/Cache/download/update.zip") == "downloaded update");
                         CHECK(Read(target / L"Updater/Cache/backup/d3dxSkinManage-Plus.exe") == "old program");
                         CHECK(fs::exists(request.extractDir / L"d3dxSkinManage-Plus.exe"));
                         CHECK(Read(target / L"Core/library.dll") == "old library");
                         CHECK(!fs::exists(target / L"launched.txt"));
                     }});
    tests.push_back({"workflow.launch_failure_keeps_update_caches", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto request = PrepareUpdate(target);
                         bool reachedLaunch = false;
                         UpdateCallbacks callbacks;
                         callbacks.step = [&](int number) {
                             if (number == 3) {
                                 reachedLaunch = true;
                                 fs::remove(target / L"d3dxSkinManage-Plus.exe");
                             }
                         };
                         CHECK(!RunUpdate(request, callbacks));
                         CHECK(reachedLaunch);
                         CHECK(Read(target / L"Updater/Cache/download/update.zip") == "downloaded update");
                         CHECK(Read(target / L"Updater/Cache/backup/d3dxSkinManage-Plus.exe") == "old program");
                         CHECK(fs::exists(request.extractDir / L"d3dxSkinManage-Plus.exe"));
                         CHECK(Read(target / L"Core/library.dll") == "new library");
                         CHECK(!fs::exists(target / L"launched.txt"));
                     }});
    tests.push_back({"workflow.download_cleanup_failure_does_not_fail_update", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto request = PrepareUpdate(target);
                         const auto package = target / L"Updater/Cache/download/update.zip";
                         const HANDLE lock = CreateFileW(package.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                                                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
                         CHECK(lock != INVALID_HANDLE_VALUE);
                         const bool updated = RunUpdate(request);
                         CloseHandle(lock);
                         CHECK(updated);
                         WaitForLaunchedFixture(target);
                         CHECK(Read(package) == "downloaded update");
                         CHECK(!fs::exists(request.extractDir));
                         CHECK(!fs::exists(target / L"Updater/Cache/backup"));
                     }});
    tests.push_back({"workflow.missing_download_cache_does_not_fail_update", [] {
                         TempDir temp;
                         const auto target = temp.path() / L"installed";
                         const auto request = PrepareUpdate(target);
                         const auto download = target / L"Updater/Cache/download";
                         CHECK(fs::remove(download / L"update.zip"));
                         CHECK(fs::remove(download));
                         CHECK(RunUpdate(request));
                         WaitForLaunchedFixture(target);
                         CHECK(!fs::exists(download));
                         CHECK(!fs::exists(request.extractDir));
                         CHECK(!fs::exists(target / L"Updater/Cache/backup"));
                     }});
}

}
