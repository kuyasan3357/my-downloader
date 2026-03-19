<?php
/**
 * Copy this file to config.php and fill in your values.
 * NEVER commit config.php to git!
 *
 * Get GitHub token at: https://github.com/settings/tokens
 * Token needs: Contents (Read & Write) + Actions (Read & Write)
 */

define('GITHUB_TOKEN', 'github_pat_xxxxxxxxxxxx');
define('GITHUB_OWNER', 'your-github-username');
define('GITHUB_REPO', 'my-downloader');
define('ACTION_SECRET', 'change-this-to-random-string');
define('DOWNLOAD_DIR', __DIR__ . '/downloads/');
