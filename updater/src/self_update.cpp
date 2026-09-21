#include "self_update.h"
#include "logger.h"
#include "paths.h"
#include "util.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <fstream>
#include <stdexcept>
#include <system_error>

namespace updater {
namespace fs = std::filesystem;
namespace {
constexpr const wchar_t* kUpdater = L"Updater/updater.exe";

// 拥有 CreateFileW 返回的文件句柄，失效值表示尚未成功打开。
struct Handle {
    HANDLE value = INVALID_HANDLE_VALUE;
    // 作用域退出时关闭有效文件句柄。
    ~Handle() {
        if (value != INVALID_HANDLE_VALUE)
            CloseHandle(value);
    }
};

// 拥有本次自更新创建的暂存路径，发布成功后由调用方清空。
struct StagingFile {
    fs::path path;
    // 异常或提前返回时尽力移除未发布文件。
    ~StagingFile() {
        if (!path.empty())
            DeleteFileW(path.c_str());
    }
};

// 将最近一次 Win32 失败转换为带操作名称的 system_error，调用前不能覆盖 GetLastError。
[[noreturn]] void FileError(const char* action) {
    throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), action);
}

// 检查普通非链接文件是否具有可执行的 x64 PE 结构及有文件数据支撑的入口点。
// 结构或读取校验失败返回 false，文件系统查询可能抛出异常；不验证签名或程序身份。
bool IsUpdaterImage(const fs::path& path) {
    if (!fs::is_regular_file(path) || IsReparsePoint(path))
        return false;
    DWORD kind = 0;
    if (!GetBinaryTypeW(path.c_str(), &kind) || kind != SCS_64BIT_BINARY)
        return false;
    std::ifstream file(path, std::ios::binary);
    const auto fileSize = fs::file_size(path);
    IMAGE_DOS_HEADER dos{};
    file.read(reinterpret_cast<char*>(&dos), sizeof(dos));
    if (!file || dos.e_magic != IMAGE_DOS_SIGNATURE || dos.e_lfanew < static_cast<LONG>(sizeof(dos)))
        return false;
    if (static_cast<unsigned long long>(dos.e_lfanew) + sizeof(IMAGE_NT_HEADERS64) > fileSize)
        return false;
    file.seekg(dos.e_lfanew);
    IMAGE_NT_HEADERS64 nt{};
    file.read(reinterpret_cast<char*>(&nt), sizeof(nt));
    const auto& header = nt.FileHeader;
    const auto& optional = nt.OptionalHeader;
    if (!file || nt.Signature != IMAGE_NT_SIGNATURE || header.Machine != IMAGE_FILE_MACHINE_AMD64 ||
        !(header.Characteristics & IMAGE_FILE_EXECUTABLE_IMAGE) || (header.Characteristics & IMAGE_FILE_DLL) ||
        optional.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC || header.NumberOfSections == 0 ||
        header.NumberOfSections > 96 || header.SizeOfOptionalHeader < sizeof(IMAGE_OPTIONAL_HEADER64))
        return false;
    const auto table = static_cast<unsigned long long>(dos.e_lfanew) + sizeof(DWORD) + sizeof(IMAGE_FILE_HEADER) +
                       header.SizeOfOptionalHeader;
    const auto tableEnd =
        table + static_cast<unsigned long long>(header.NumberOfSections) * sizeof(IMAGE_SECTION_HEADER);
    if (tableEnd > optional.SizeOfHeaders || optional.SizeOfHeaders > fileSize || !optional.AddressOfEntryPoint ||
        optional.AddressOfEntryPoint >= optional.SizeOfImage ||
        (optional.Subsystem != IMAGE_SUBSYSTEM_WINDOWS_GUI && optional.Subsystem != IMAGE_SUBSYSTEM_WINDOWS_CUI))
        return false;
    if (optional.NumberOfRvaAndSizes > IMAGE_DIRECTORY_ENTRY_SECURITY) {
        const auto& certificate = optional.DataDirectory[IMAGE_DIRECTORY_ENTRY_SECURITY];
        if (certificate.Size &&
            (certificate.VirtualAddress < optional.SizeOfHeaders ||
             static_cast<unsigned long long>(certificate.VirtualAddress) + certificate.Size > fileSize))
            return false;
    }
    file.seekg(static_cast<std::streamoff>(table));
    bool entryIsBacked = false;
    for (WORD index = 0; index < header.NumberOfSections; ++index) {
        IMAGE_SECTION_HEADER section{};
        file.read(reinterpret_cast<char*>(&section), sizeof(section));
        if (!file)
            return false;
        if (section.SizeOfRawData &&
            (section.PointerToRawData < optional.SizeOfHeaders ||
             static_cast<unsigned long long>(section.PointerToRawData) + section.SizeOfRawData > fileSize))
            return false;
        if (static_cast<unsigned long long>(section.VirtualAddress) +
                std::max(section.Misc.VirtualSize, section.SizeOfRawData) >
            optional.SizeOfImage)
            return false;
        if ((section.Characteristics & IMAGE_SCN_MEM_EXECUTE) &&
            optional.AddressOfEntryPoint >= section.VirtualAddress &&
            optional.AddressOfEntryPoint <
                static_cast<unsigned long long>(section.VirtualAddress) + section.SizeOfRawData) {
            entryIsBacked = true;
        }
    }
    return entryIsBacked;
}

// 组合 PID、Unix 毫秒时间和进程内序号生成同目录候选路径，并执行 SafeChild 校验。
// 此处不占用名称，调用方须用独占创建或不覆盖的改名操作处理碰撞。
fs::path UniqueSibling(const fs::path& install, const wchar_t* suffix) {
    static std::atomic<unsigned long long> serial{0};
    const auto stamp =
        std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch())
            .count();
    return SafeChild(install, std::wstring(kUpdater) + suffix + std::to_wstring(GetCurrentProcessId()) + L"." +
                                  std::to_wstring(stamp) + L"." + std::to_wstring(serial++));
}

// 独占创建暂存文件并流式复制、刷新内容；成功创建后将清理责任交给 staged。
// 所有读写失败均抛出异常，文件句柄在返回或抛出前关闭。
void Stage(const fs::path& source, const fs::path& install, StagingFile& staged) {
    Handle input{CreateFileW(source.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                             FILE_FLAG_SEQUENTIAL_SCAN, nullptr)};
    if (input.value == INVALID_HANDLE_VALUE)
        FileError("Open new updater");
    Handle output;
    for (int attempt = 0; attempt < 8; ++attempt) {
        const auto path = UniqueSibling(install, L".new_update.");
        output.value = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_NEW,
                                   FILE_ATTRIBUTE_NORMAL, nullptr);
        if (output.value != INVALID_HANDLE_VALUE) {
            staged.path = path;
            break;
        }
        if (GetLastError() != ERROR_FILE_EXISTS && GetLastError() != ERROR_ALREADY_EXISTS)
            FileError("Stage updater");
    }
    if (staged.path.empty())
        throw std::runtime_error("Cannot reserve updater staging file");
    std::array<char, 65536> buffer{};
    for (;;) {
        DWORD count = 0;
        if (!ReadFile(input.value, buffer.data(), static_cast<DWORD>(buffer.size()), &count, nullptr))
            FileError("Read updater");
        if (!count)
            break;
        DWORD offset = 0;
        while (offset < count) {
            DWORD written = 0;
            if (!WriteFile(output.value, buffer.data() + offset, count - offset, &written, nullptr))
                FileError("Write updater");
            if (!written)
                throw std::runtime_error("Updater staging write made no progress");
            offset += written;
        }
    }
    if (!FlushFileBuffers(output.value))
        FileError("Flush staged updater");
}

// 只接受 updater.exe.old_update 后附三个非空十进制字段的名称，避免误删其他文件。
bool IsRetiredName(const std::wstring& filename) {
    const auto name = PathKey(filename);
    const std::wstring prefix = L"updater.exe.old_update.";
    if (name.compare(0, prefix.size(), prefix) != 0)
        return false;
    int groups = 1;
    bool digit = false;
    for (size_t index = prefix.size(); index < name.size(); ++index) {
        if (name[index] == L'.') {
            if (!digit || ++groups > 3)
                return false;
            digit = false;
        } else if (name[index] >= L'0' && name[index] <= L'9')
            digit = true;
        else
            return false;
    }
    return digit && groups == 3;
}
}

UpdaterReplacement::~UpdaterReplacement() {
    if (!committed_)
        Rollback();
}

bool UpdaterReplacement::Install(const fs::path& extractDir, const fs::path& installDir) {
    try {
        if (published_ || !retired_.empty() || committed_)
            throw std::logic_error("Updater transaction already used");
        ValidateUpdateDirectories(extractDir, installDir);
        const auto source = SafeChild(extractDir, kUpdater);
        if (!fs::exists(source))
            return true; // 更新包可不附带更新器。
        if (!IsUpdaterImage(source))
            throw std::runtime_error("Package updater is not a valid native x64 executable");
        destination_ = SafeChild(installDir, kUpdater);
        fs::create_directories(destination_.parent_path());
        if (fs::exists(destination_) && !fs::is_regular_file(destination_)) {
            throw std::invalid_argument("Updater destination is not a regular file");
        }
        StagingFile staged;
        Stage(source, installDir, staged);
        if (!IsUpdaterImage(staged.path))
            throw std::runtime_error("Staged updater validation failed");

        // 新旧内容相同也先移开旧映像，保持文件替换流程一致。
        // 旧进程继续使用已映射的映像，直到正常退出。
        if (fs::exists(destination_)) {
            for (int attempt = 0; attempt < 8; ++attempt) {
                const auto retired = UniqueSibling(installDir, L".old_update.");
                if (MoveFileExW(destination_.c_str(), retired.c_str(), MOVEFILE_WRITE_THROUGH)) {
                    retired_ = retired;
                    break;
                }
                const DWORD error = GetLastError();
                if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS)
                    continue;
                if (attempt < 2 && (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION ||
                                    error == ERROR_LOCK_VIOLATION)) {
                    Sleep(100);
                    continue;
                }
                FileError("Retire old updater");
            }
            if (retired_.empty())
                throw std::runtime_error("Cannot allocate retired updater name");
        }
        if (!MoveFileExW(staged.path.c_str(), destination_.c_str(), MOVEFILE_WRITE_THROUGH))
            FileError("Publish new updater");
        staged.path.clear();
        published_ = true;
        LogInfo(L"已释放新更新器: " + destination_.wstring());
        if (!retired_.empty())
            LogInfo(L"旧更新器退出后清理: " + retired_.wstring());
        return true;
    } catch (const std::exception& error) {
        LogError(L"更新器自更新失败: " + ExceptionMessage(error));
        Rollback();
        return false;
    }
}

void UpdaterReplacement::Commit() noexcept {
    committed_ = true;
}

bool UpdaterReplacement::Rollback() noexcept {
    if (committed_)
        return true;
    try {
        if (destination_.empty())
            return true;
        const auto install = destination_.parent_path().parent_path();
        SafeChild(install, kUpdater);
        if (!retired_.empty())
            SafeChild(install, L"Updater/" + retired_.filename().wstring());
        if (published_) {
            if (!DeleteFileW(destination_.c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND) {
                FileError("Remove uncommitted updater");
            }
            published_ = false;
        }
        if (!retired_.empty()) {
            if (!MoveFileExW(retired_.c_str(), destination_.c_str(), MOVEFILE_WRITE_THROUGH))
                FileError("Restore old updater");
            retired_.clear();
            LogInfo(L"已恢复旧更新器");
        }
        return true;
    } catch (const std::exception& error) {
        LogError(L"恢复旧更新器失败，保留恢复文件: " + ExceptionMessage(error));
    } catch (...) {
    }
    return false;
}

void CleanupRetiredUpdaters(const fs::path& installDir) {
    try {
        const auto current = SafeChild(installDir, kUpdater);
        // 保留中断替换后唯一可用的更新器。
        if (!IsUpdaterImage(current))
            return;
        for (const auto& entry : fs::directory_iterator(current.parent_path())) {
            if (!IsRetiredName(entry.path().filename().wstring()) || IsReparsePoint(entry.path()))
                continue;
            const auto path = SafeChild(installDir, L"Updater/" + entry.path().filename().wstring());
            if (!IsUpdaterImage(path))
                continue;
            const DWORD attributes = GetFileAttributesW(path.c_str());
            if (attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_READONLY)) {
                SetFileAttributesW(path.c_str(), attributes & ~FILE_ATTRIBUTE_READONLY);
            }
            if (DeleteFileW(path.c_str()))
                LogInfo(L"已清理旧更新器: " + path.filename().wstring());
            else
                LogInfo(L"旧更新器仍被占用，保留待下次清理: " + path.filename().wstring());
        }
    } catch (const std::exception& error) {
        LogWarning(L"旧更新器清理未完成: " + ExceptionMessage(error));
    }
}
}
