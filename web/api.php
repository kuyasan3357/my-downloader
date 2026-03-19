<?php
/**
 * API - Triggers GitHub Actions and tracks task status
 */

require_once __DIR__ . '/config.php';

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

$TASKS_FILE = __DIR__ . '/tasks.json';

function load_tasks() {
    global $TASKS_FILE;
    if (!file_exists($TASKS_FILE)) return [];
    $data = json_decode(file_get_contents($TASKS_FILE), true);
    return is_array($data) ? $data : [];
}

function save_tasks($tasks) {
    global $TASKS_FILE;
    $tasks = array_slice($tasks, -100, 100, true);
    file_put_contents($TASKS_FILE, json_encode($tasks, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT), LOCK_EX);
}

function github_api($method, $endpoint, $body = null) {
    $url = "https://api.github.com/repos/" . GITHUB_OWNER . "/" . GITHUB_REPO . $endpoint;
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . GITHUB_TOKEN,
            'Accept: application/vnd.github.v3+json',
            'User-Agent: VideoDownloader-Web',
            'Content-Type: application/json',
        ],
        CURLOPT_TIMEOUT => 30,
    ]);
    if ($method === 'POST') {
        curl_setopt($ch, CURLOPT_POST, true);
        if ($body) curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body));
    }
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['code' => $httpCode, 'body' => json_decode($response, true)];
}

function human_filesize($bytes) {
    if ($bytes >= 1048576) return round($bytes / 1048576, 1) . ' MB';
    if ($bytes >= 1024) return round($bytes / 1024, 0) . ' KB';
    return $bytes . ' B';
}

$action = $_GET['action'] ?? '';

switch ($action) {

    // ─── Trigger download ───────────────────────────────────
    case 'download':
        if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
            http_response_code(405);
            echo json_encode(['error' => 'POST required']);
            exit;
        }

        $input = json_decode(file_get_contents('php://input'), true);
        $videoUrl = trim($input['url'] ?? '');
        $format = $input['format'] ?? 'best_video';

        if (empty($videoUrl)) {
            http_response_code(400);
            echo json_encode(['error' => 'URL is required']);
            exit;
        }

        $allowedFormats = ['best_video', 'mp4_1080', 'mp4_720', 'mp3_audio'];
        if (!in_array($format, $allowedFormats)) {
            http_response_code(400);
            echo json_encode(['error' => 'Invalid format']);
            exit;
        }

        $taskId = 'web_' . time() . '_' . substr(md5(uniqid()), 0, 8);

        $callbackUrl = (isset($_SERVER['HTTPS']) ? 'https' : 'http') . '://'
            . $_SERVER['HTTP_HOST']
            . dirname($_SERVER['SCRIPT_NAME'])
            . '/api.php?action=callback';

        // Trigger via repository_dispatch
        $result = github_api('POST', '/dispatches', [
            'event_type' => 'download_video',
            'client_payload' => [
                'video_url' => $videoUrl,
                'format' => $format,
                'task_id' => $taskId,
                'callback_url' => $callbackUrl,
            ],
        ]);

        if ($result['code'] === 204 || $result['code'] === 200) {
            $tasks = load_tasks();
            $tasks[$taskId] = [
                'id' => $taskId,
                'url' => $videoUrl,
                'format' => $format,
                'status' => 'queued',
                'message' => 'Waiting for GitHub Actions...',
                'created_at' => time(),
                'run_id' => null,
                'files' => [],
            ];
            save_tasks($tasks);
            echo json_encode(['success' => true, 'task_id' => $taskId]);
        } else {
            http_response_code(502);
            echo json_encode([
                'error' => 'Failed to trigger GitHub Actions',
                'details' => $result['body']['message'] ?? 'Unknown error',
            ]);
        }
        break;

    // ─── Check task status ──────────────────────────────────
    case 'status':
        $taskId = $_GET['task_id'] ?? '';
        if (empty($taskId)) {
            http_response_code(400);
            echo json_encode(['error' => 'task_id required']);
            exit;
        }

        $tasks = load_tasks();
        $task = $tasks[$taskId] ?? null;

        if (!$task) {
            http_response_code(404);
            echo json_encode(['error' => 'Task not found']);
            exit;
        }

        // Poll GitHub Actions if still pending
        if (in_array($task['status'], ['queued', 'running'])) {
            // Check in_progress runs
            $runs = github_api('GET', '/actions/runs?per_page=5&status=in_progress');
            if ($runs['code'] === 200 && !empty($runs['body']['workflow_runs'])) {
                foreach ($runs['body']['workflow_runs'] as $run) {
                    $runTime = strtotime($run['created_at']);
                    if (abs($runTime - $task['created_at']) < 300) {
                        $task['status'] = 'running';
                        $task['run_id'] = $run['id'];
                        $task['message'] = 'Downloading on GitHub server...';
                        break;
                    }
                }
            }

            // Check completed runs
            $completed = github_api('GET', '/actions/runs?per_page=5&status=completed');
            if ($completed['code'] === 200) {
                foreach ($completed['body']['workflow_runs'] ?? [] as $run) {
                    $runTime = strtotime($run['created_at']);
                    if (abs($runTime - $task['created_at']) < 300) {
                        $task['run_id'] = $run['id'];
                        if ($run['conclusion'] === 'success') {
                            $task['status'] = 'completed';
                            $task['message'] = 'Download completed!';
                        } elseif ($run['conclusion'] === 'failure') {
                            $task['status'] = 'failed';
                            $task['message'] = 'Download failed. Check Actions logs.';
                        }
                        break;
                    }
                }
            }

            $tasks[$taskId] = $task;
            save_tasks($tasks);
        }

        // Check local files (uploaded via FTP)
        $files = [];
        $downloadDir = DOWNLOAD_DIR . $taskId . '/';
        if (is_dir($downloadDir)) {
            foreach (scandir($downloadDir) as $f) {
                if ($f === '.' || $f === '..' || $f === 'results.json') continue;
                $fpath = $downloadDir . $f;
                if (is_file($fpath)) {
                    $files[] = [
                        'filename' => $f,
                        'size_human' => human_filesize(filesize($fpath)),
                        'download_url' => 'downloads/' . $taskId . '/' . rawurlencode($f),
                    ];
                }
            }
        }

        echo json_encode([
            'task_id' => $task['id'],
            'status' => $task['status'],
            'message' => $task['message'],
            'run_id' => $task['run_id'],
            'files' => $files,
            'actions_url' => $task['run_id']
                ? "https://github.com/" . GITHUB_OWNER . "/" . GITHUB_REPO . "/actions/runs/" . $task['run_id']
                : null,
        ]);
        break;

    // ─── Callback from GitHub Actions ───────────────────────
    case 'callback':
        if ($_SERVER['REQUEST_METHOD'] !== 'POST') { http_response_code(405); exit; }

        $secret = $_SERVER['HTTP_X_ACTION_SECRET'] ?? '';
        if ($secret !== ACTION_SECRET) {
            http_response_code(403);
            echo json_encode(['error' => 'Invalid secret']);
            exit;
        }

        $input = json_decode(file_get_contents('php://input'), true);
        $taskId = $input['task_id'] ?? '';
        if (!empty($taskId)) {
            $tasks = load_tasks();
            if (isset($tasks[$taskId])) {
                $tasks[$taskId]['status'] = ($input['status'] === 'success') ? 'completed' : 'failed';
                $tasks[$taskId]['run_id'] = $input['run_id'] ?? null;
                $tasks[$taskId]['message'] = ($input['status'] === 'success') ? 'Download completed!' : 'Download failed.';
                save_tasks($tasks);
            }
        }
        echo json_encode(['ok' => true]);
        break;

    default:
        echo json_encode(['endpoints' => ['download', 'status', 'callback']]);
}
