property editorURL : "http://127.0.0.1:8765"

on editorIsRunning()
	try
		do shell script "/usr/bin/curl --silent --fail --max-time 1 " & quoted form of (editorURL & "/api/projects") & " | /usr/bin/grep -q '\"projects\"'"
		return true
	on error
		return false
	end try
end editorIsRunning

on run
	if not editorIsRunning() then
		set launchCommand to "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; " & ¬
			"editor_root=\"${LLM_VIDEO_EDITOR_ROOT:-$HOME/coding-projects/llm-video-editor}\"; " & ¬
			"python=\"$editor_root/.venv/bin/python\"; [ -x \"$python\" ] || python=/opt/homebrew/bin/python3; " & ¬
			"/bin/mkdir -p \"$HOME/Library/Logs/LLM Video Editor\"; " & ¬
			"/usr/bin/nohup \"$python\" \"$editor_root/run.py\" ui --no-open " & ¬
			">>\"$HOME/Library/Logs/LLM Video Editor/editor.log\" 2>&1 </dev/null &"
		do shell script launchCommand

		repeat 40 times
			delay 0.1
			if editorIsRunning() then exit repeat
		end repeat
	end if

	if editorIsRunning() then
		open location editorURL
	else
		display alert "LLM Video Editor could not start." message "See ~/Library/Logs/LLM Video Editor/editor.log for details." as critical
	end if
end run
