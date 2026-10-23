// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LLMVideoEditorApp",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "LLMVideoEditorApp", targets: ["LLMVideoEditorApp"]),
    ],
    targets: [
        .executableTarget(
            name: "LLMVideoEditorApp",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("CoreImage"),
                .linkedFramework("QuartzCore"),
                .linkedFramework("WebKit"),
            ]
        ),
    ],
    swiftLanguageModes: [.v5]
)
