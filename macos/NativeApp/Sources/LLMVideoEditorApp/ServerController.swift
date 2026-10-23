import Foundation

final class ServerController {
    private let root: URL
    private let baseURL: URL
    private var process: Process?
    private var logHandle: FileHandle?

    init(root: URL, baseURL: URL) {
        self.root = root
        self.baseURL = baseURL
    }

    func ensureRunning(completion: @escaping (Result<Void, Error>) -> Void) {
        ping { [weak self] running in
            guard let self else { return }
            if running {
                completion(.success(()))
                return
            }
            do {
                try self.start()
                self.poll(attempt: 0, completion: completion)
            } catch {
                completion(.failure(error))
            }
        }
    }

    private func ping(completion: @escaping (Bool) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/projects"))
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { _, response, _ in
            let running = (response as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { completion(running) }
        }.resume()
    }

    private func poll(attempt: Int, completion: @escaping (Result<Void, Error>) -> Void) {
        guard attempt < 80 else {
            completion(.failure(NSError(
                domain: "LLMVideoEditor",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "The Python editor server did not start. Check ~/Library/Logs/LLM Video Editor/editor.log."]
            )))
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            self?.ping { running in
                if running { completion(.success(())) }
                else { self?.poll(attempt: attempt + 1, completion: completion) }
            }
        }
    }

    private func start() throws {
        let virtualPython = root.appendingPathComponent(".venv/bin/python")
        let python = FileManager.default.isExecutableFile(atPath: virtualPython.path)
            ? virtualPython
            : URL(fileURLWithPath: "/usr/bin/python3")
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/LLM Video Editor", isDirectory: true)
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("editor.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: logURL)
        try handle.seekToEnd()
        logHandle = handle

        let process = Process()
        process.executableURL = python
        process.arguments = [root.appendingPathComponent("run.py").path, "ui", "--no-open"]
        process.currentDirectoryURL = root
        process.standardOutput = handle
        process.standardError = handle
        process.standardInput = FileHandle.nullDevice
        try process.run()
        self.process = process
    }
}
