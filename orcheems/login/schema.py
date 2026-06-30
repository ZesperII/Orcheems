import json
import uuid

from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional


PPJ_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "ppj everyflow automation namespace")


class Credential(BaseModel):
    """ 
    Schema định nghĩa credential - thông tin đăng nhập và các dữ liệu liên quan cần thiết để thực hiện login vào một site nào đó.
    
    Args:
        site:   tên site / dịch vụ mà credential này dùng để đăng nhập, không phải full URL, chỉ là định danh (e.g. "vnpt", "google", etc.). 
                Dùng để gọi các login service tương ứng đã thiết lập/tự thiết kế logic đăng nhập cho site đó.
        base_url: URL đích mà credential này liên kết (e.g. URL trang đăng nhập).
        type: loại credential (e.g. "basic", "oauth", "api_key", etc.) — có thể được dịch vụ đăng nhập sử dụng để xác định cách sử dụng dữ liệu. 
                Không ảnh hưởng đến logic đăng nhập, chỉ mang tính chất tham khảo, nhưng có ảnh hưởng đến cách `credential_id` được tạo ra.
        data: dict linh hoạt để chứa bất kỳ thông tin cần thiết nào cho việc đăng nhập (e.g. username, password, client_id, client_secret, etc.)
        
    Returns:
        credential_id: UUID v5 được tạo ra từ (site, base_url, type và data), toàn bộ credential đều được sử dụng để xây dựng `credential_id`. Đây là định danh duy nhất cho credential này, được sử dụng để quản lý session tương ứng sau khi đăng nhập thành công.
    """
    
    site        : str
    base_url    : str
    type        : str            = Field(default="not-mentioned")
    data        : Dict[str, Any] = Field(default_factory=dict)
    
    model_config = { "extra": "allow" }
        
    @property
    def credential_id(self) -> str:
        """ 
        UUID v5 định danh duy nhất cho bộ Credential này.
        Deterministic - cùng input luôn cho ra cùng output.
        Không reversible - không thể recover data/base_url từ credential_id.
        
        Chỉ ảnh hưởng bởi `site`, `base_url` và `data`, không phụ thuộc vào các trường khác (nếu có thêm trong tương lai và extra fields).
        """
        
        raw = json.dumps(
            {
                "site": self.site,
                "base_url": self.base_url,
                "data": self.data,
            },
            sort_keys = True,
            ensure_ascii = False
        )
        
        return str(uuid.uuid5(PPJ_NAMESPACE, raw))

@dataclass
class LoginResult:
    """ 
    Result object returned by login services after performing login logic.
    
    Args:
        success: indicates if login was successful
        credential_id: ID of the credential used for login, it can be any string that helps identify the credential of site (e.g. username, email, or a generated ID). This is useful for logging and debugging.
        site: name of the site or service, not full URL, just an identifier (e.g. "vnpt", "google", etc.)
        base_url: base URL of the site or service, this is full URL that the login service attempted to log in to (e.g. "https://coatsphongphuhcm-tt78.vnpt-invoice.com.vn/")
        metadata: additional metadata related to the login attempt
        error: error message if login failed
    """
    
    success         : bool
    credential_id   : str
    site            : str
    base_url        : str
    metadata        : Dict[str, Any] = field(default_factory=dict)
    error           : Optional[str] = None