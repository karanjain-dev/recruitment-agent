import os
from dotenv import load_dotenv
load_dotenv()
if __name__ == '__main__':
    import uvicorn
    target = 'app.hosted:app' if os.getenv('EVAL_MOUNTED') == '1' else 'app.main:app'
    uvicorn.run(target, host='0.0.0.0', port=int(os.getenv('PORT', '8000')), workers=1, proxy_headers=True, forwarded_allow_ips='*')
