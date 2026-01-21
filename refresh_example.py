#!/usr/bin/env python3
"""
Cloudflare Cookie 强制刷新示例脚本

这个脚本演示如何使用强制刷新功能来获取最新的 CF Cookie。
"""

import asyncio
import aiohttp
import json
from typing import Optional, Dict, Any


class CacheFreshener:
    """CF Cookie 缓存刷新工具"""
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        self.server_url = server_url
    
    async def refresh_cookies(
        self, 
        url: str, 
        proxy: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        强制刷新指定 URL 的 Cloudflare Cookie。
        
        Args:
            url: 目标 URL
            proxy: 可选的代理 URL
            
        Returns:
            刷新结果字典或 None
        """
        async with aiohttp.ClientSession() as session:
            refresh_url = f"{self.server_url}/cache/refresh"
            params = {"url": url}
            
            if proxy:
                params["proxy"] = proxy
            
            try:
                async with session.post(refresh_url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error = await resp.json()
                        print(f"❌ 刷新失败 (状态码: {resp.status})")
                        print(f"   错误: {error.get('detail', '未知错误')}")
                        return None
            except Exception as e:
                print(f"❌ 请求失败: {e}")
                return None
    
    async def get_cookies(self, url: str) -> Optional[Dict[str, Any]]:
        """
        获取 URL 的 Cookie（使用缓存）。
        
        Args:
            url: 目标 URL
            
        Returns:
            Cookie 信息或 None
        """
        async with aiohttp.ClientSession() as session:
            cookies_url = f"{self.server_url}/cookies"
            params = {"url": url}
            
            try:
                async with session.get(cookies_url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error = await resp.json()
                        print(f"❌ 获取失败 (状态码: {resp.status})")
                        print(f"   错误: {error.get('detail', '未知错误')}")
                        return None
            except Exception as e:
                print(f"❌ 请求失败: {e}")
                return None
    
    async def get_cache_stats(self) -> Optional[Dict[str, Any]]:
        """获取缓存统计信息"""
        async with aiohttp.ClientSession() as session:
            stats_url = f"{self.server_url}/cache/stats"
            
            try:
                async with session.get(stats_url) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        print(f"❌ 获取统计失败 (状态码: {resp.status})")
                        return None
            except Exception as e:
                print(f"❌ 请求失败: {e}")
                return None


async def demo_basic_refresh():
    """基础刷新演示"""
    print("=" * 60)
    print("演示 1: 基础 Cookie 刷新")
    print("=" * 60)
    
    freshener = CacheFreshener()
    url = "https://javdb.com"
    
    print(f"\n📌 目标网址: {url}")
    print("\n⏳ 正在强制刷新 Cookie (这可能需要 10-30 秒)...\n")
    
    result = await freshener.refresh_cookies(url)
    
    if result and result["status"] == "success":
        print(f"✅ 刷新成功!")
        print(f"   主机名: {result['hostname']}")
        print(f"   Cookie 数: {result['cookies_count']}")
        print(f"   耗时: {result['generation_time_ms']}ms")
        print(f"   User-Agent: {result['user_agent'][:50]}...")
    else:
        print("❌ 刷新失败")


async def demo_multiple_refreshes():
    """多个网址刷新演示"""
    print("\n" + "=" * 60)
    print("演示 2: 多个网址刷新（顺序执行）")
    print("=" * 60)
    
    freshener = CacheFreshener()
    urls = [
        "https://example.com",
        "https://test-site.com",
    ]
    
    for url in urls:
        print(f"\n📌 正在刷新: {url}")
        result = await freshener.refresh_cookies(url)
        
        if result and result["status"] == "success":
            print(f"   ✅ 成功 - {result['cookies_count']} 个 Cookie - {result['generation_time_ms']}ms")
        else:
            print(f"   ❌ 失败")
        
        print(f"   ⏳ 等待 2 秒...")
        await asyncio.sleep(2)


async def demo_with_proxy():
    """使用代理刷新演示"""
    print("\n" + "=" * 60)
    print("演示 3: 使用代理刷新")
    print("=" * 60)
    
    freshener = CacheFreshener()
    url = "https://example.com"
    proxy = "http://proxy-server:8080"  # 修改为你的代理地址
    
    print(f"\n📌 目标网址: {url}")
    print(f"📌 代理: {proxy}")
    print("\n⏳ 正在通过代理刷新 Cookie...\n")
    
    result = await freshener.refresh_cookies(url, proxy)
    
    if result and result["status"] == "success":
        print(f"✅ 通过代理刷新成功!")
        print(f"   主机名: {result['hostname']}")
        print(f"   Cookie 数: {result['cookies_count']}")
        print(f"   耗时: {result['generation_time_ms']}ms")
    else:
        print("❌ 刷新失败")


async def demo_cache_operations():
    """缓存操作演示"""
    print("\n" + "=" * 60)
    print("演示 4: 缓存操作")
    print("=" * 60)
    
    freshener = CacheFreshener()
    url = "https://example.com"
    
    # 1. 查看缓存统计
    print("\n1️⃣ 查看缓存统计:")
    stats = await freshener.get_cache_stats()
    if stats:
        print(f"   活跃缓存数: {stats['cached_entries']}")
        print(f"   总主机数: {stats['total_hostnames']}")
        if stats['hostnames']:
            print(f"   缓存的主机: {', '.join(stats['hostnames'][:3])}...")
    
    # 2. 刷新 Cookie
    print(f"\n2️⃣ 强制刷新 {url} 的 Cookie:")
    result = await freshener.refresh_cookies(url)
    if result and result["status"] == "success":
        print(f"   ✅ 成功 - Cookie 数: {result['cookies_count']}")
    
    # 3. 获取刷新后的 Cookie
    print(f"\n3️⃣ 获取刷新后的 Cookie:")
    cookies_result = await freshener.get_cookies(url)
    if cookies_result:
        print(f"   ✅ 成功 - Cookie 数: {len(cookies_result.get('cookies', {}))}")
        # 显示前几个 Cookie
        cookies = cookies_result.get('cookies', {})
        for i, (name, value) in enumerate(list(cookies.items())[:3]):
            print(f"      - {name}: {value[:30]}...")
    
    # 4. 再次查看缓存
    print(f"\n4️⃣ 更新后的缓存统计:")
    stats = await freshener.get_cache_stats()
    if stats:
        print(f"   活跃缓存数: {stats['cached_entries']}")
        print(f"   总主机数: {stats['total_hostnames']}")


async def main():
    """主函数"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 58 + "║")
    print("║" + "  Cloudflare Cookie 强制刷新 - 使用示例".center(58) + "║")
    print("║" + " " * 58 + "║")
    print("╚" + "=" * 58 + "╝")
    
    print("\n📝 注意: 确保 Cloudflare Bypasser 服务器正在运行 (localhost:8000)")
    print("   如果运行在不同的地址，请修改 CacheFreshener 的初始化参数")
    
    # 选择要运行的演示
    print("\n选择演示:")
    print("  1. 基础 Cookie 刷新")
    print("  2. 多个网址刷新")
    print("  3. 使用代理刷新")
    print("  4. 缓存操作")
    print("  5. 运行所有演示")
    
    choice = input("\n请选择 (1-5): ").strip()
    
    try:
        if choice == "1":
            await demo_basic_refresh()
        elif choice == "2":
            await demo_multiple_refreshes()
        elif choice == "3":
            print("\n⚠️  请确保代理地址正确，否则会失败")
            await demo_with_proxy()
        elif choice == "4":
            await demo_cache_operations()
        elif choice == "5":
            print("\n⚠️  运行所有演示可能需要较长时间\n")
            await demo_basic_refresh()
            await asyncio.sleep(3)
            await demo_cache_operations()
        else:
            print("❌ 无效的选择")
            return
        
        print("\n" + "=" * 60)
        print("✅ 演示完成!")
        print("=" * 60 + "\n")
        
    except KeyboardInterrupt:
        print("\n\n⚠️  已取消")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
