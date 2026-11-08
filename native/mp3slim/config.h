/* lame_config.h —— libmp3lame 在 Android(NDK/clang) 上的最小 config.h
 *
 * LAME 原本靠 autotools 生成 config.h。p4a 的 recipe 里没必要为它跑一遍
 * configure（交叉编译时 configure 还会瞎猜 long double、iconv 之类），
 * 直接把需要打开的开关写死：编译期用 -DHAVE_CONFIG_H 生效。
 *
 * 只保留 libmp3lame/ 里真正会读的宏（研究过每个 #ifdef），
 * 多写反而容易踩「猜错类型」的坑。
 */
#ifndef LAME_CONFIG_H
#define LAME_CONFIG_H

#define HAVE_CONFIG_H 1

/* 头文件/函数探测：Android bionic 全都有 */
#define HAVE_ERRNO_H 1
#define HAVE_FCNTL_H 1
#define HAVE_INTTYPES_H 1
#define HAVE_STDINT_H 1
#define HAVE_LIMITS_H 1
#define HAVE_MEMORY_H 1
#define HAVE_STDLIB_H 1
#define HAVE_STRING_H 1
#define HAVE_STRINGS_H 1
#define HAVE_UNISTD_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_MPGLIB 1
#define LAME_LIBRARY_BUILD 1
#define STDC_HEADERS 1

/* ieee754 类型：配置脚本发现没有现成 typedef 时由 config.h 补上，
 * util.h 直接用它声明 fast_log2()，缺了就是 unknown type name。 */
typedef float ieee754_float32_t;
typedef double ieee754_float64_t;

/* 位宽：arm64 上 long / 指针都是 8 字节 */
#define SIZEOF_DOUBLE 8
#define SIZEOF_FLOAT 4
#define SIZEOF_INT 4
#define SIZEOF_LONG 8
#define SIZEOF_LONG_DOUBLE 16
#define SIZEOF_LONG_LONG 8
#define SIZEOF_SHORT 2
#define SIZEOF_UNSIGNED_INT 4
#define SIZEOF_UNSIGNED_LONG 8
#define SIZEOF_UNSIGNED_LONG_LONG 8
#define SIZEOF_UNSIGNED_SHORT 2

#define PACKAGE "lame"
#define PACKAGE_NAME "lame"
#define PACKAGE_VERSION "3.100"
#define VERSION "3.100"

#endif /* LAME_CONFIG_H */
