// SPDX-License-Identifier: Apache-2.0
import SwiftUI
import UIKit

/// 模板自检页。
///
/// 保留它的原因：这条链路涉及证书、签名、描述文件、CI、TestFlight 五层，
/// 任何一层配错都可能"看起来成功了但装上的不是新包"。
/// 把版本号、build 号、Bundle ID 直接打在首屏，装上就能核对——
/// 比去设置里翻半天快得多。
struct ContentView: View {
    private var version: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }

    private var build: String {
        Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
    }

    private var bundleID: String {
        Bundle.main.bundleIdentifier ?? "?"
    }

    private var device: String {
        UIDevice.current.model
    }

    private var osVersion: String {
        UIDevice.current.systemVersion
    }

    var body: some View {
        NavigationStack {
            List {
                Section("已安装的包") {
                    row("版本", version)
                    row("构建号", build)
                    row("Bundle ID", bundleID)
                }

                Section("运行环境") {
                    row("设备", device)
                    row("系统", "iOS \(osVersion)")
                }

                Section {
                    Label("看到这页说明整条链路已打通：\n证书 → 签名 → CI 构建 → TestFlight",
                          systemImage: "checkmark.seal.fill")
                        .foregroundStyle(.green)
                        .font(.footnote)
                }
            }
            .navigationTitle("自检")
        }
    }

    private func row(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
    }
}

#Preview {
    ContentView()
}
