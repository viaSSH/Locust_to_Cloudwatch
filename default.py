import time
from locust import HttpUser, task, between

class QuickstartUser(HttpUser):
    wait_time = between(1, 5)

    @task
    def main_page(self):
        self.client.get("/")
        self.client.get("/docs")
        self.client.post("/api/user/signin", json={"email":"abc@def.com", "password":"1q2w3e"})
        

    # @task(3)
    # def post_method(self):
    #     self.client.post("/api/user/signin", json={"email":"locust@gscdn.com", "password":"test1test1"})

    # @task(3)
    # def view_items(self):
    #     for item_id in range(10):
    #         self.client.get(f"/item?id={item_id}", name="/item")
    #         time.sleep(1)

    # def on_start(self):
    #     self.client.post("/api/user/signin", json={"email":"locust@gscdn.com", "password":"test1test1"})
