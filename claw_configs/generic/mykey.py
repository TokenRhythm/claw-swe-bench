import os

OPENROUTER_BASE_URL = os.environ.get('OPENROUTER_BASE_URL', '').strip().rstrip('/')
if not OPENROUTER_BASE_URL:
    raise RuntimeError('OPENROUTER_BASE_URL is not set; export your API base URL before running.')


native_oai_config_glm51_openrouter = {
    'name': 'glm-5.1-openrouter',
    'apikey': os.environ.get('OPENROUTER_API_KEY', ''),
    'apibase': OPENROUTER_BASE_URL,
    'model': 'z-ai/glm-5.1',
    'max_retries': 3,
    'connect_timeout': 10,
    'read_timeout': 180,
}


native_oai_config_dsv41flash_openrouter = {
    'name': 'dsv41flash-openrouter',
    'apikey': os.environ.get('OPENROUTER_API_KEY', ''),
    'apibase': OPENROUTER_BASE_URL,
    'model': 'deepseek/deepseek-v4.1-flash',
    'reasoning_effort': 'xhigh',
    'max_retries': 3,
    'connect_timeout': 10,
    'read_timeout': 180,
}


native_oai_config_qwen36flash_openrouter = {
    'name': 'qwen3.6-flash-openrouter',
    'apikey': os.environ.get('OPENROUTER_API_KEY', ''),
    'apibase': OPENROUTER_BASE_URL,
    'model': 'qwen/qwen3.6-flash',
    'max_retries': 3,
    'connect_timeout': 10,
    'read_timeout': 180,
}
