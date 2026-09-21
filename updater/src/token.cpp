#include "token.h"

#include <windows.h>
#include <array>
#include <bcrypt.h>
#include <ctime>
#include <exception>
#include <vector>

namespace updater {

namespace {
// 拥有 BCrypt 算法提供程序；其生命周期覆盖依赖它的哈希对象。
struct Algorithm {
    BCRYPT_ALG_HANDLE handle = nullptr;
    // 关闭已成功打开的提供程序，空句柄无操作。
    ~Algorithm() {
        if (handle)
            BCryptCloseAlgorithmProvider(handle, 0);
    }
};
// 拥有 BCrypt 哈希句柄；外部对象缓冲区须保持有效直至该句柄被销毁。
struct Hash {
    BCRYPT_HASH_HANDLE handle = nullptr;
    // 先销毁哈希句柄，再由外层作用域释放缓冲区与算法提供程序。
    ~Hash() {
        if (handle)
            BCryptDestroyHash(handle);
    }
};
}

bool VerifyLaunchToken(const std::wstring& token, std::int64_t now) {
    try {
        const auto separator = token.find(L'.');
        if (separator == std::wstring::npos || !separator || token.find(L'.', separator + 1) != std::wstring::npos ||
            token.size() - separator - 1 != 64)
            return false;

        const auto timestampText = token.substr(0, separator);
        size_t consumed = 0;
        const auto timestamp = std::stoll(timestampText, &consumed, 16);
        if (consumed != timestampText.size() || timestamp < 0 || now < 0)
            return false;
        const auto age = timestamp > now ? timestamp - now : now - timestamp;
        if (age > 120)
            return false;

        std::string ascii;
        for (const auto c : timestampText) {
            if (c > 0x7f)
                return false;
            ascii.push_back(static_cast<char>(c));
        }
        Algorithm algorithm;
        if (BCryptOpenAlgorithmProvider(&algorithm.handle, BCRYPT_SHA256_ALGORITHM, nullptr,
                                        BCRYPT_ALG_HANDLE_HMAC_FLAG) < 0)
            return false;
        DWORD objectSize = 0;
        DWORD received = 0;
        if (BCryptGetProperty(algorithm.handle, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&objectSize),
                              sizeof(objectSize), &received, 0) < 0)
            return false;
        std::vector<unsigned char> object(objectSize);
        // 须与主程序 APP_UPDATE_TOKEN_SECRET 的值一致。
        unsigned char secret[] = "d3dxSkinManagePlus_Updater_LaunchToken_2026!";
        Hash hash;
        if (BCryptCreateHash(algorithm.handle, &hash.handle, object.data(), objectSize, secret,
                             static_cast<ULONG>(sizeof(secret) - 1), 0) < 0)
            return false;
        if (BCryptHashData(hash.handle, reinterpret_cast<PUCHAR>(ascii.data()), static_cast<ULONG>(ascii.size()), 0) <
            0)
            return false;
        std::array<unsigned char, 32> digest{};
        if (BCryptFinishHash(hash.handle, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0)
            return false;

        constexpr wchar_t hex[] = L"0123456789abcdef";
        unsigned difference = 0;
        for (size_t i = 0; i < digest.size(); ++i) {
            difference |= static_cast<unsigned>(token[separator + 1 + 2 * i] ^ hex[digest[i] >> 4]);
            difference |= static_cast<unsigned>(token[separator + 2 + 2 * i] ^ hex[digest[i] & 15]);
        }
        return difference == 0;
    } catch (const std::exception&) {
        return false;
    }
}

bool VerifyLaunchToken(const std::wstring& token) {
    return VerifyLaunchToken(token, static_cast<std::int64_t>(std::time(nullptr)));
}

}
