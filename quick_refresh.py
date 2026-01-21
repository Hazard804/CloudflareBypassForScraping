#!/usr/bin/env python3
"""
简化版 - Cloudflare Cookie 强制刷新交互式工具

快速、简洁的交互式脚本，用于刷新和查看 Cookie
"""

import asyncio
import aiohttp
import json
from typing import Optional, Dict, Any
from urllib.parse import urlparse


async def refresh_and_display(url: str, proxy: Optional[str] = None):
    """刷新 Cookie 并显示结果"""
    async with aiohttp.ClientSession() as session:
        try:
            print(f"\n⏳ 正在刷新 {url}...")
            if proxy:
                print(f"   代理: {proxy}")
            print("   请稍候...\n")
            
            params = {"url": url}
            if proxy:
                params["proxy"] = proxy
            
            async with session.post(
                "http://localhost:8000/cache/refresh",
                params=params,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    
                    # 显示成功信息
                    print("✅ 刷新成功！\n")
                    print("━" * 50)
                    print(f"主机名: {result.get('hostname')}")
                    print(f"Cookie 数: {result.get('cookies_count')} 个")
                    print(f"耗时: {result.get('generation_time_ms')}ms ({result.get('generation_time_ms')/1000:.1f}秒)")
                    print("━" * 50)
                    
                    # 显示 User-Agent
                    if result.get('user_agent'):
                        print(f"\nUser-Agent: {result.get('user_agent')[:60]}...")
                    
                    # 询问是否保存
                    save = input("\n💾 是否保存结果到文件? (y/n): ").strip().lower()
                    if save in ('y', 'yes', '1'):
                        with open(f"cookie_{urlparse(url).netloc}.json", 'w') as f:
                            json.dump(result, f, indent=2)
                            print(f"✅ 已保存到 cookie_{urlparse(url).netloc}.json")
                    
                    return True
                else:
                    error = await resp.json()
                    print(f"❌ 错误: {error.get('detail', '未知错误')}")
                    return False
                    
        except asyncio.TimeoutError:
            print("❌ 超时 - 刷新耗时过长")
            return False
        except aiohttp.ClientConnectorError:
            print("❌ 无法连接到服务器 (http://localhost:8000)")
            print("   请确保服务器已启动: python server.py")
            return False
        except Exception as e:
            print(f"❌ 错误: {e}")
            return False


async def main():
    """主程序"""
    print("\n" + "="*50)
    print("  Cloudflare Cookie 强制刷新 - 交互式工具")
    print("="*50 + "\n")
    
    while True:
        # 获取 URL
        url = input("📌 输入网址 (或 'quit' 退出): ").strip()
        
        if url.lower() in ('quit', 'exit', 'q'):
            print("\n👋 再见！\n")
            break
        
        if not url:
            print("❌ 网址不能为空\n")
            continue
        
        # 格式化 URL
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # 验证 URL
        try:
            urlparse(url)
        except:
            print("❌ URL 格式无效\n")
            continue
        
        # 询问代理
        use_proxy = input("🔀 是否使用代理? (y/n): ").strip().lower()
        proxy = None
        
        if use_proxy in ('y', 'yes', '1'):
            proxy = input("   代理地址 (如 http://proxy:8080): ").strip()
            if not proxy:
                proxy = None
            elif not proxy.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
                print("❌ 代理格式错误\n")
                continue
        
        # 刷新
        success = await refresh_and_display(url, proxy)
        
        if success:
            # 询问是否继续
            cont = input("\n▶️ 继续刷新其他网址? (y/n): ").strip().lower()
            if cont not in ('y', 'yes', '1'):
                print("\n👋 再见！\n")
                break
        else:
            # 失败后继续
            cont = input("\n▶️ 重试? (y/n): ").strip().lower()
            if cont not in ('y', 'yes', '1'):
                print("\n👋 再见！\n")
                break
        
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  已取消\n")
