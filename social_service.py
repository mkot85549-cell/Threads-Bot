import time

import requests


class ThreadsFarmService:
    MAX_TEXT_LENGTH = 500  # Threads text limit

    def __init__(self, user_id: str, access_token: str, proxy: str = None):
        self.threads_user_id = user_id
        self.access_token = access_token
        self.base_url = "https://graph.threads.net/v1.0"
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json; charset=utf-8"
        })

    @staticmethod
    def _json(resp: requests.Response) -> dict:
        """Always returns a dict; non-JSON bodies become {'error': {...}}."""
        try:
            data = resp.json()
        except ValueError:
            return {"error": {"message": f"non-JSON response (HTTP {resp.status_code})"}}
        return data if isinstance(data, dict) else {"error": {"message": "unexpected response shape"}}

    def publish_post(self, text: str, image_url: str = None, reply_to_id: str = None) -> str:
        """
        Publishes a new post OR replies to an existing post/comment.
        Returns a string: starts with '✅' on success, anything else means NOT published.
        """
        if not text or not text.strip():
            return "❌ Empty text — not published"
        if len(text) > self.MAX_TEXT_LENGTH:
            return f"❌ Text exceeds {self.MAX_TEXT_LENGTH} characters — not published"

        try:
            container_url = f"{self.base_url}/{self.threads_user_id}/threads"
            payload = {"text": text, "access_token": self.access_token}

            if image_url:
                payload["media_type"] = "IMAGE"
                payload["image_url"] = image_url
            else:
                payload["media_type"] = "TEXT"

            if reply_to_id:
                payload["reply_to_id"] = reply_to_id

            # Step 1: create container (json= keeps UTF-8 correct)
            response = self._session.post(container_url, json=payload, proxies=self.proxies, timeout=20)
            container_data = self._json(response)
            container_id = container_data.get("id")

            if not container_id:
                return f"❌ Container error: {container_data}"

            # Step 1.5: poll container status for images until FINISHED
            if image_url:
                if not self._wait_for_container(container_id):
                    return (
                        f"❌ Media container {container_id} not ready "
                        f"(Meta failed to process the image). Post NOT published."
                    )

            # Step 2: publish container
            publish_url = f"{self.base_url}/{self.threads_user_id}/threads_publish"
            pub_payload = {"creation_id": container_id, "access_token": self.access_token}

            pub_response = self._session.post(publish_url, json=pub_payload, proxies=self.proxies, timeout=20)
            pub_data = self._json(pub_response)

            if "id" in pub_data:
                return f"✅ Success (ID: {pub_data['id']})"
            return f"❌ Publishing error: {pub_data}"

        except Exception as e:
            return f"Critical network error: {str(e)}"

    def _wait_for_container(self, container_id: str,
                            max_attempts: int = 12, delay: int = 3) -> bool:
        """
        Polls the media container until Meta finishes processing it.
        Status values: IN_PROGRESS, FINISHED, ERROR, EXPIRED. True only on FINISHED.
        """
        status_url = f"{self.base_url}/{container_id}"
        params = {"fields": "status,error_message", "access_token": self.access_token}

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self._session.get(status_url, params=params, proxies=self.proxies, timeout=20)
                data = self._json(resp)
            except Exception as e:
                print(f"⚠️ [Media] Error checking container status (attempt {attempt}): {e}")
                time.sleep(delay)
                continue

            status = data.get("status")

            if status == "FINISHED":
                print(f"✅ [Media] Container {container_id} ready (FINISHED) in ~{attempt * delay}s.")
                return True
            if status == "ERROR":
                print(f"❌ [Media] Meta failed to process image: {data.get('error_message')}")
                return False
            if status == "EXPIRED":
                print(f"❌ [Media] Container {container_id} expired (EXPIRED).")
                return False

            print(f"⏳ [Media] Container {container_id}: status={status} (attempt {attempt}/{max_attempts})…")
            time.sleep(delay)

        print(f"⏰ [Media] Container {container_id} timed out before reaching FINISHED.")
        return False

    def search_posts_with_ids(self, keyword: str, limit: int = 25,
                              search_type: str = "RECENT") -> list:
        """
        Public keyword search: GET /keyword_search.

        Requires the `threads_keyword_search` permission approved by Meta App Review.
        WITHOUT approval Meta silently searches only the authenticated user's OWN
        posts — so callers MUST filter out their own posts before replying.
        (The old /{user_id}/threads?q=… call was just the account's own feed.)

        Returns [{'id', 'text', 'username'}, ...]; [] on any error.
        """
        try:
            resp = self._session.get(
                f"{self.base_url}/keyword_search",
                params={
                    "q": keyword,
                    "search_type": search_type,      # TOP | RECENT
                    "search_mode": "KEYWORD",
                    "fields": "id,text,username",
                    "limit": max(1, min(limit, 100)),
                    "access_token": self.access_token,
                },
                proxies=self.proxies,
                timeout=20,
            )
            data = self._json(resp)
            if "error" in data:
                print(f"❌ [Search] keyword_search error: {data['error']}")
                return []

            posts = []
            for item in data.get("data", []):
                if item.get("id") and item.get("text"):
                    posts.append({
                        "id": item["id"],
                        "text": item["text"],
                        "username": item.get("username") or "",
                    })
            return posts
        except Exception as e:
            print(f"❌ Error searching posts: {e}")
            return []

    def search_posts_by_keyword(self, keyword: str, limit: int = 5) -> list:
        """Texts only (topic inspiration). Same endpoint and caveats as above."""
        return [p["text"] for p in self.search_posts_with_ids(keyword, limit=limit)]