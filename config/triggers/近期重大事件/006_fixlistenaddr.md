🛠️ 五、把地址改对

1️⃣ Lagrange  
• 在 appsettings.json 中修改 Host 字段，把监听地址从 0.0.0.0 改成 127.0.0.1 或 localhost
• 改完后重启 Lagrange，一样不要把这些端口裸放公网 

2️⃣ Napcat  
• Linux 终端输入 napcat 打开 TUI，编辑账号配置
• 服务监听 Host 改为 127.0.0.1 或 localhost  
• WebUI 建议关闭，若开必须设 Token/密码  
• Windows 不推荐部署 Napcat（检测点密集）  

3️⃣ Sealdice  
• WebUI 必须设强密码  
• 连接 OneBot 时只填 127.0.0.1/localhost，不要用公网 IP  
• 如果设置了 Access Token：两端都要写，保持一致