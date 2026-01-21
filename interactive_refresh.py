#!/usr/bin/env python3
"""
Cloudflare Cookie 强制刷新 - 交互式测试脚本

这是一个用户友好的交互式脚本，允许用户：
1. 输入指定的网址
2. 可选输入代理
3. 强制刷新该网址的 Cloudflare Cookie
4. 查看刷新结果和 Cookie 信息
"""

import asyncio
import aiohttp
import json
import sys
from typing import Optional, Dict, Any
from datetime import datetime
from urllib.parse import urlparse


class CookieRefresherCLI:
    """交互式 Cookie 刷新工具"""
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        self.server_url = server_url
        self.session = None
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        if self.session:
            await self.session.close()
    
    async def refresh_cookies(self, url: str, proxy: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """刷新指定 URL 的 Cookie"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        try:
            params = {"url": url}
            if proxy:
                params["proxy"] = proxy
            
            print(f"\n⏳ 正在刷新 {url}...")
            if proxy:
                print(f"   使用代理: {proxy}")
            print("   请稍候，这可能需要 10-30 秒...\n")
            
            async with self.session.post(
                f"{self.server_url}/cache/refresh",
                params=params,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    error_data = await resp.json()
                    print(f"❌ 错误 (状态码: {resp.status})")
                    print(f"   {error_data.get('detail', '未知错误')}")
                    return None
        except asyncio.TimeoutError:
            print("❌ 请求超时 - 服务器响应时间过长")
            return None
        except aiohttp.ClientConnectorError:
            print(f"❌ 无法连接到服务器 ({self.server_url})")
            print("   请确保服务器已启动")
            return None
        except Exception as e:
            print(f"❌ 请求失败: {e}")
            return None
    
    async def get_cookies(self, url: str) -> Optional[Dict[str, Any]]:
        """获取 URL 的 Cookie"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        try:
            params = {"url": url}
            async with self.session.get(
                f"{self.server_url}/cookies",
                params=params,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return None
        except Exception as e:
            return None
    
    def display_result(self, result: Dict[str, Any]):
        """美化显示刷新结果"""
        print("\n" + "="*70)
        print("✅ Cookie 刷新成功！")
        print("="*70)
        
        # 基本信息
        print(f"\n📌 目标网址信息:")
        print(f"   主机名: {result.get('hostname', 'N/A')}")
        
        # Cookie 信息
        cookies_count = result.get('cookies_count', 0)
        print(f"\n🍪 Cookie 信息:")
        print(f"   总数: {cookies_count} 个")
        
        # User-Agent
        user_agent = result.get('user_agent', '')
        if user_agent:
            print(f"\n🌐 User-Agent:")
            print(f"   {user_agent}")
        
        # 性能信息
        generation_time = result.get('generation_time_ms', 0)
        print(f"\n⏱️  性能:")
        print(f"   生成耗时: {generation_time} ms ({generation_time/1000:.1f} 秒)")
        
        print("\n" + "="*70)
    
    def display_cookies(self, url: str, cookies: Dict[str, str]):
        """美化显示 Cookie 信息"""
        print("\n" + "="*70)
        print("🍪 获取的 Cookie 详情")
        print("="*70)
        
        hostname = urlparse(url).netloc
        print(f"\n📌 网址: {hostname}")
        print(f"   总 Cookie 数: {len(cookies)}\n")
        
        # 分类显示 Cookie
        cf_cookies = {}
        other_cookies = {}
        
        for name, value in cookies.items():
            if name.startswith(('cf_', '__cf')):
                cf_cookies[name] = value
            else:
                other_cookies[name] = value
        
        # 显示 Cloudflare 相关 Cookie
        if cf_cookies:
            print("🔐 Cloudflare Cookie:")
            for name, value in cf_cookies.items():
                value_preview = value[:40] + "..." if len(value) > 40 else value
                print(f"   • {name}: {value_preview}")
        
        # 显示其他 Cookie
        if other_cookies:
            print(f"\n📦 其他 Cookie ({len(other_cookies)} 个):")
            for name, value in list(other_cookies.items())[:5]:
                value_preview = value[:40] + "..." if len(value) > 40 else value
                print(f"   • {name}: {value_preview}")
            
            if len(other_cookies) > 5:
                print(f"   ... 还有 {len(other_cookies) - 5} 个")
        
        print("\n" + "="*70)
    
    def validate_url(self, url: str) -> bool:
        """验证 URL 格式"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except:
            return False
    
    def format_url(self, url: str) -> str:
        """格式化 URL（添加协议前缀）"""
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url


def print_header():
    """打印欢迎信息"""
    print("\n")
    print("╔" + "="*68 + "╗")
    print("║" + " "*68 + "║")
    print("║" + "  Cloudflare Cookie 强制刷新 - 交互式工具".center(68) + "║")
    print("║" + " "*68 + "║")
    print("╚" + "="*68 + "╝")
    print()


def print_menu():
    """打印菜单"""
    print("\n🎯 请选择操作:")
    print("   1. 刷新单个网址的 Cookie")
    print("   2. 查看已缓存的 Cookie")
    print("   3. 批量刷新")
    print("   4. 返回主菜单")
    print("   0. 退出")
    print()


def get_yes_no(prompt: str) -> bool:
    """获取是/否输入"""
    while True:
        response = input(prompt).strip().lower()
        if response in ('y', 'yes', '是', '1'):
            return True
        elif response in ('n', 'no', '否', '0'):
            return False
        else:
            print("   ❌ 请输入 y/n (是/否)")


async def refresh_single_url(cli: CookieRefresherCLI):
    """刷新单个网址"""
    print("\n📝 请输入要刷新的网址:")
    print("   例如: example.com 或 https://example.com")
    url = input("   网址: ").strip()
    
    if not url:
        print("   ❌ 网址不能为空")
        return
    
    url = cli.format_url(url)
    
    if not cli.validate_url(url):
        print(f"   ❌ 网址格式无效: {url}")
        return
    
    # 可选代理
    use_proxy = get_yes_no("   是否使用代理? (y/n): ")
    proxy = None
    
    if use_proxy:
        print("\n   请输入代理地址:")
        print("   例如: http://proxy:8080 或 socks5://proxy:1080")
        proxy = input("   代理: ").strip()
        
        if not proxy:
            print("   ℹ️ 未输入代理，将不使用代理")
            proxy = None
        elif not proxy.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            print("   ❌ 代理格式错误，将不使用代理")
            proxy = None
    
    # 刷新 Cookie
    result = await cli.refresh_cookies(url, proxy)
    
    if result and result.get('status') == 'success':
        cli.display_result(result)
        
        # 询问是否查看 Cookie 详情
        if get_yes_no("\n   是否查看详细的 Cookie 信息? (y/n): "):
            cookies_result = await cli.get_cookies(url)
            if cookies_result:
                cli.display_cookies(url, cookies_result.get('cookies', {}))
        
        # 保存到文件
        if get_yes_no("\n   是否保存结果到文件? (y/n): "):
            save_result_to_file(url, result)


async def view_cached_cookies(cli: CookieRefresherCLI):
    """查看已缓存的 Cookie"""
    print("\n📝 请输入要查看的网址:")
    url = input("   网址: ").strip()
    
    if not url:
        print("   ❌ 网址不能为空")
        return
    
    url = cli.format_url(url)
    
    if not cli.validate_url(url):
        print(f"   ❌ 网址格式无效: {url}")
        return
    
    print("\n⏳ 正在获取 Cookie...")
    
    cookies_result = await cli.get_cookies(url)
    
    if cookies_result:
        cli.display_cookies(url, cookies_result.get('cookies', {}))
    else:
        print("   ⚠️ 未找到缓存的 Cookie（可能需要先刷新）")


async def batch_refresh(cli: CookieRefresherCLI):
    """批量刷新"""
    print("\n📝 请输入要刷新的网址列表 (每行一个，输入空行结束):")
    print("   例如:")
    print("      example.com")
    print("      test.com")
    print("      site.com")
    print()
    
    urls = []
    while True:
        url = input(f"   [{len(urls)+1}] 网址: ").strip()
        if not url:
            break
        url = cli.format_url(url)
        if cli.validate_url(url):
            urls.append(url)
        else:
            print(f"   ❌ 网址格式无效，跳过: {url}")
    
    if not urls:
        print("   ❌ 没有输入有效的网址")
        return
    
    print(f"\n将要刷新 {len(urls)} 个网址")
    
    # 可选代理
    use_proxy = get_yes_no("   是否为所有网址使用同一代理? (y/n): ")
    proxy = None
    
    if use_proxy:
        proxy = input("   代理: ").strip()
        if not proxy:
            proxy = None
        elif not proxy.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            print("   ❌ 代理格式错误")
            proxy = None
    
    results = []
    
    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] 正在处理: {urlparse(url).netloc}")
        result = await cli.refresh_cookies(url, proxy)
        
        if result and result.get('status') == 'success':
            results.append({
                'url': url,
                'hostname': result.get('hostname'),
                'cookies_count': result.get('cookies_count'),
                'time_ms': result.get('generation_time_ms'),
                'status': 'success'
            })
            print(f"   ✅ 成功 - {result.get('cookies_count')} 个 Cookie - {result.get('generation_time_ms')}ms")
        else:
            results.append({
                'url': url,
                'status': 'failed'
            })
            print(f"   ❌ 失败")
        
        # 等待 2 秒避免过快
        if i < len(urls):
            await asyncio.sleep(2)
    
    # 显示总结
    print("\n" + "="*70)
    print("📊 批量刷新总结")
    print("="*70)
    
    success_count = sum(1 for r in results if r['status'] == 'success')
    failed_count = len(results) - success_count
    
    print(f"\n   总数: {len(results)}")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ❌ 失败: {failed_count}")
    
    if success_count > 0:
        total_time = sum(r.get('time_ms', 0) for r in results if r['status'] == 'success')
        avg_time = total_time // success_count if success_count > 0 else 0
        print(f"\n   ⏱️  平均耗时: {avg_time}ms")
        print(f"   总耗时: {total_time}ms ({total_time/1000:.1f} 秒)")
    
    print("\n" + "="*70)
    
    # 保存结果
    if get_yes_no("\n   是否保存结果到文件? (y/n): "):
        save_batch_results(results)


def save_result_to_file(url: str, result: Dict[str, Any]):
    """保存单个结果到文件"""
    try:
        filename = f"cookie_refresh_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        data = {
            'timestamp': datetime.now().isoformat(),
            'url': url,
            'hostname': result.get('hostname'),
            'cookies_count': result.get('cookies_count'),
            'user_agent': result.get('user_agent'),
            'generation_time_ms': result.get('generation_time_ms')
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"\n   ✅ 结果已保存到: {filename}")
    except Exception as e:
        print(f"\n   ❌ 保存失败: {e}")


def save_batch_results(results: list):
    """保存批量结果到文件"""
    try:
        filename = f"cookie_refresh_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        data = {
            'timestamp': datetime.now().isoformat(),
            'total': len(results),
            'success': sum(1 for r in results if r['status'] == 'success'),
            'failed': sum(1 for r in results if r['status'] == 'failed'),
            'results': results
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        print(f"\n   ✅ 结果已保存到: {filename}")
    except Exception as e:
        print(f"\n   ❌ 保存失败: {e}")


async def main():
    """主函数"""
    print_header()
    
    print("⚙️  初始化...")
    print("   检查服务器连接...")
    
    async with CookieRefresherCLI() as cli:
        # 测试连接
        try:
            async with aiohttp.ClientSession() as test_session:
                async with test_session.get(
                    "http://localhost:8000/cache/stats",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        print("   ⚠️  服务器连接异常")
                        print("\n💡 提示: 确保已启动服务器")
                        print("   运行: python server.py")
                        return
                    print("   ✅ 服务器正常")
        except Exception as e:
            print(f"   ❌ 无法连接到服务器")
            print(f"   错误: {e}")
            print("\n💡 请先启动服务器:")
            print("   python server.py")
            return
        
        # 主菜单循环
        while True:
            print_menu()
            choice = input("请选择 (0-4): ").strip()
            
            if choice == '1':
                await refresh_single_url(cli)
            elif choice == '2':
                await view_cached_cookies(cli)
            elif choice == '3':
                await batch_refresh(cli)
            elif choice == '4':
                continue
            elif choice == '0':
                print("\n👋 感谢使用，再见！\n")
                break
            else:
                print("   ❌ 无效的选择，请重新输入")
            
            # 菜单间隔
            input("\n   按 Enter 继续...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  已取消\n")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)
